"""The reconciler (T093; design 5.2 to 5.5): it makes the registry's releases true.

For every app it works toward two facts the registry holds: the ``active`` release should be
running with its route open, and the ``target_release_no`` release, when it is ``pending``, should
become the new ``active`` one. The way up is fixed and every step is a status the registry shows::

    pending -> building -> starting -> healthy -> active

* **environment**: refused first, so a release that needs a resource the app lacks fails at once
  and nothing is built for it;
* **build** (``ReleaseBuilder``), then **admission** (``admission.py``) and **start**
  (``ChildLauncher``, which is ``sandbox/launcher.py`` in production);
* **health**: the process is polled from the second it starts (``health.py``), and a process that
  exits first fails the release at once instead of at the deadline;
* **swap** (``swap.py``): one registry transaction, then the route. This module never opens or
  closes a route itself; it only calls the swap, so "a route opens only after the health check
  passed" (constitution II) has exactly one place to be wrong.

A release that fails before health leaves the running release, its child and its route alone
(FR-004) and stores the reason and the end of the child's log (FR-002). ``failed`` is final: no
later pass retries it. A release interrupted by a restart (``building``, ``starting`` or
``healthy`` with nothing running) goes back to ``pending`` and is built again from the stored
snapshot, which is safe because nothing was ever routed to it. At start-up the ``active`` release
of each app is restored the same way, health check included, with no status change when it
succeeds; if it cannot be, it is marked ``failed`` and its route stays closed.

One asyncio task per app does that app's work in order, so a slow build never holds up another
app's route (FR-008) and two Updates of one app never run at once. ``wake`` asks for a pass; a
pass compares the registry with what runs and does only what differs, so it is cheap to call
often. A pass is skipped while the registry reports an error: acting on a snapshot that may be
stale could stop a child that should run.

A child that dies while its route is open is restarted, the same release, after 1, 2, 4, 8 and
16 seconds, and then the app is ``crashed`` (``restart.py``, FR-007); no older release is ever
started in its place. ``request_restart`` starts a new copy of the active release beside the running
one and swaps to it, and ``app_renamed`` does the same under a new slug (FR-010): if the new copy
does not come up the old one keeps serving, and nothing in the registry changes. Activating an older
release and editing the config are rows the admin routes write (``activate.py``,
``registry/operations.py``); this loop then brings them up like any other pending release.
``stop_app`` is the last step before an app's rows are deleted: it takes the running copy down and
closes the route, writes nothing, and leaves the rest to the pass that follows the delete.

Not here (other tasks): the retention cap (T114).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from appmux.appyaml.appyaml import substitute_command_arguments
from appmux.reconciler.admission import (
    MemoryLedger,
    RunningApp,
    SecondCopy,
    check_admission,
    memory_ledger,
)
from appmux.reconciler.app_control import RunState
from appmux.reconciler.build import BuildFailed, ReleaseBuilder
from appmux.reconciler.child_env import ChildEnvironmentFactory, EnvironmentRefused
from appmux.reconciler.children import (
    CHILD_START_TIMEOUT_S,
    CHILD_STOP_GRACE_S,
    DEFAULT_CHILD_PORT,
    ChildExit,
    ChildLauncher,
    ChildStart,
    ChildStartError,
    RunningChild,
)
from appmux.reconciler.clock import Clock
from appmux.reconciler.health import HealthProbe, HealthResult, wait_until_healthy
from appmux.reconciler.rename import under_new_slug
from appmux.reconciler.restart import CrashReport, RestartBudget
from appmux.reconciler.route_table import RouteTable
from appmux.reconciler.swap import DRAIN_S, ConnectionCloser, LiveCopy, Swap
from appmux.registry.errors import RegistryError, RegistryUnavailable
from appmux.registry.models import App, AppConfig, Release
from appmux.registry.protocol import Registry
from appmux.registry.releases import clean_log_tail, clean_reason, release_status_change
from appmux.registry.snapshot import RegistrySnapshot

logger = logging.getLogger(__name__)

# How often the watcher looks at the registry's snapshot for a change (design 5.2: in memory only).
DEFAULT_WATCH_INTERVAL_S: Final = 5.0
# A value shorter than this is not hidden in stored text: it would match ordinary words.
MIN_REDACTED_VALUE_CHARS: Final = 8
_REDACTED: Final = "***"
_INTERRUPTED_STATUSES: Final = frozenset({"building", "starting", "healthy"})
# Design 5.4: what a runtime runs when the manifest gives no command.
_DEFAULT_COMMANDS: Final[Mapping[str, tuple[str, ...]]] = {
    "python": ("python", "app.py"),
    "node": ("npm", "run", "start"),
}
_NO_COMMAND: Final = (
    "the release has no start command and no known runtime; add a command to its app.yaml"
)
_INTERNAL_ERROR: Final = "internal error while releasing; the appmux log has the details"


class _Refused(Exception):
    """A step decided the release cannot go on; the reason (and log) is stored on the release."""

    def __init__(self, reason: str, log_tail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.log_tail = log_tail


@dataclass(slots=True)
class _Copy:
    """A running copy, the memory budget it was admitted with and what a crash report needs.

    ``environment`` is kept only to take its values out of a crash's log; ``since_s`` is when the
    copy went live (``restart.STABLE_RUN_S``); ``watcher`` waits for the child to exit.
    """

    live: LiveCopy
    budget_mb: int
    environment: Mapping[str, str] = field(default_factory=dict, repr=False)
    since_s: float = 0.0
    watcher: asyncio.Task[None] | None = None


@dataclass(slots=True)
class _Incoming:
    """The copy being brought up: its booking in admission control, and its child once started."""

    budget_mb: int
    health_deadline_s: int
    started_at_s: float | None = None
    child: RunningChild | None = None


@dataclass(slots=True, eq=False)
class _AppState:
    """What this process runs for one app. Only the app's own task writes it."""

    slug: str
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    is_busy: bool = False
    live: _Copy | None = None
    incoming: _Incoming | None = None
    draining: list[_Copy] = field(default_factory=list)
    # Crash handling (restart.py). ``needs_restart``: the live copy died and a restart is due;
    # ``is_crashed``: the restarts are used up, so nothing runs until a person acts.
    budget: RestartBudget = field(default_factory=RestartBudget)
    needs_restart: bool = False
    is_crashed: bool = False
    crash: CrashReport | None = None
    # A restart was asked for (or the app was renamed): start a new copy beside the live one.
    restart_requested: bool = False
    restart_failure: str | None = None
    # Set by ``app_renamed``; the app's own task then hands its copy to the task of the new slug.
    renamed_to: str | None = None
    # Callers of ``stop_app`` waiting for the app's task to take it down; each is set once it did.
    stop_waiters: list[asyncio.Event] = field(default_factory=list)


@dataclass(slots=True)
class _Attempt:
    """One release on its way up (or being restored), with what a failure needs to clean up."""

    state: _AppState
    app: App
    config: AppConfig
    row: Release
    is_restore: bool
    environment: Mapping[str, str] = field(default_factory=dict)
    child: RunningChild | None = None
    # A second copy of the release that is already running, or a copy replacing one that died.
    # Its failure is not the release's: the registry row keeps the status it has.
    is_restart: bool = False


class Reconciler:
    def __init__(
        self,
        *,
        registry: Registry,
        routes: RouteTable,
        launcher: ChildLauncher,
        builder: ReleaseBuilder,
        environments: ChildEnvironmentFactory,
        probe: HealthProbe,
        closer: ConnectionCloser,
        clock: Clock,
        memory_limit_mb: int,
        memory_reserve_mb: Callable[[], int],
        child_port: int = DEFAULT_CHILD_PORT,
        start_timeout_s: float = CHILD_START_TIMEOUT_S,
        stop_grace_s: float = CHILD_STOP_GRACE_S,
        drain_s: float = DRAIN_S,
        watch_interval_s: float | None = DEFAULT_WATCH_INTERVAL_S,
    ) -> None:
        self._registry = registry
        self._launcher = launcher
        self._builder = builder
        self._environments = environments
        self._probe = probe
        self._clock = clock
        self._memory_limit_mb = memory_limit_mb
        self._memory_reserve_mb = memory_reserve_mb
        self._child_port = child_port
        self._start_timeout_s = start_timeout_s
        self._stop_grace_s = stop_grace_s
        self._watch_interval_s = watch_interval_s
        self._swap = Swap(
            registry=registry,
            routes=routes,
            closer=closer,
            clock=clock,
            drain_s=drain_s,
            stop_grace_s=stop_grace_s,
        )
        self._states: dict[str, _AppState] = {}
        self._watcher: asyncio.Task[None] | None = None
        self._is_started = False
        self._is_stopping = False
        # Set when a pass was skipped because the registry was in error; the watcher then wakes
        # every app once the registry works again, even if its snapshot object did not change.
        self._was_held_back = False
        self._idle = asyncio.Event()
        self._idle.set()

    # --- control ---------------------------------------------------------------------------

    async def start(self) -> None:
        """Begin reconciling every app in the registry, and keep watching it for changes."""
        if self._is_started:
            return
        self._is_started = True
        self.wake_all()
        if self._watch_interval_s is not None:
            self._watcher = asyncio.create_task(
                self._watch(self._watch_interval_s), name="reconciler-watch"
            )

    def wake(self, slug: str) -> None:
        """Ask for a pass over ``slug``; call it after anything that may change what it needs."""
        if not self._is_started or self._is_stopping:
            return
        state = self._states.get(slug)
        if state is None:
            state = self._spawn(slug)
        state.wake.set()
        self._idle.clear()

    def wake_all(self) -> None:
        slugs = {app.slug for app in self._registry.snapshot.apps} | set(self._states)
        for slug in sorted(slugs):
            self.wake(slug)

    async def wait_idle(self) -> None:
        """Return when no app is mid-pass or has a pass waiting (for tests and for shutdown)."""
        await self._idle.wait()

    def run_state(self, slug: str, desired_state: str) -> RunState:
        """How ``slug`` runs now: ``stopped``, ``starting`` (also while it waits to be restarted),
        ``running`` (a copy has its route) or ``crashed`` (the restarts are used up)."""
        if desired_state == "stopped":
            return "stopped"
        state = self._states.get(slug)
        if state is None:
            return "starting"
        if state.is_crashed:
            return "crashed"
        return "running" if state.live is not None else "starting"

    def request_restart(self, slug: str) -> None:
        """Bring a new copy of the active release up beside the running one, then swap to it.

        If the app has no running copy it gets one attempt with a fresh restart count. A copy that
        does not come up leaves the running one serving and the registry as it is; the reason is
        ``last_restart_failure``.
        """
        self.wake(slug)
        state = self._states.get(slug)
        if state is not None:
            state.restart_requested = True

    def app_renamed(self, old_slug: str, new_slug: str) -> None:
        """The registry now calls ``old_slug`` ``new_slug``: move its copy there and restart it.

        Call it right after the rename was written. The running copy is served under the new slug
        at once, and a new copy is started under that name beside it (FR-010); the old slug has no
        route from then on.
        """
        if not self._is_started or self._is_stopping:
            return
        state = self._states.get(old_slug)
        if state is None:
            self.wake(new_slug)
            return
        state.renamed_to = new_slug
        self.wake(old_slug)

    async def stop_app(self, slug: str) -> None:
        """Close ``slug``'s route and stop its child, then return; no registry row changes.

        The app's own task does it, because only that task writes what runs: the call waits for
        the pass in hand (a build or a health check, say) and then for the take-down. A task that
        is waiting out a crash restart's backoff is waited for too. An app this process does not
        know, or a reconciler that is not running, has nothing to stop.
        """
        state = self._states.get(slug)
        if state is None or not self._is_started or self._is_stopping:
            return
        stopped = asyncio.Event()
        state.stop_waiters.append(stopped)
        self.wake(slug)
        await stopped.wait()

    def memory_ledger(self) -> MemoryLedger:
        """What the slot's memory holds now: the same books admission control reads."""
        return memory_ledger(
            limit_mb=self._memory_limit_mb,
            reserve_mb=self._memory_reserve_mb(),
            running=self._books(),
            now_s=self._clock.monotonic(),
        )

    def last_crash(self, slug: str) -> CrashReport | None:
        """The last crash of ``slug`` since it last ran on purpose, with its exit and log tail."""
        state = self._states.get(slug)
        return None if state is None else state.crash

    def last_restart_failure(self, slug: str) -> str | None:
        """Why the last copy started beside the running one did not come up, if it did not."""
        state = self._states.get(slug)
        return None if state is None else state.restart_failure

    async def stop_all(self, within_s: float) -> None:
        """Close every route and stop every child, all at once, within ``within_s`` seconds.

        Registry statuses are left as they are: an ``active`` release stays ``active`` so the next
        start restores it, and a release caught mid-way is reset to ``pending`` by the next pass.
        Whatever is still running when the budget ends is logged and left to the container.
        """
        self._is_stopping = True
        if self._watcher is not None:
            self._watcher.cancel()
        for slug, state in self._states.items():
            self._swap.close(slug)
            for stopped in state.stop_waiters:
                stopped.set()  # its task is about to be cancelled, so nobody would set it
        workers = [state.task for state in self._states.values() if state.task is not None]
        for state in self._states.values():
            for copy in (state.live, *state.draining):
                if copy is not None and copy.watcher is not None:
                    copy.watcher.cancel()
                    workers.append(copy.watcher)
        for worker in workers:
            worker.cancel()
        grace_s = min(self._stop_grace_s, within_s / 2)
        stops = [self._stop_with_grace(child, grace_s) for child in self._tracked_children()]
        try:
            await asyncio.wait_for(
                asyncio.gather(*workers, *stops, return_exceptions=True), within_s
            )
        except TimeoutError:
            unfinished = sorted(
                state.slug
                for state in self._states.values()
                if state.task is not None and not state.task.done()
            )
            logger.error(
                "stop_all ran out of its %.0f s budget; still stopping: %s",
                within_s,
                ", ".join(unfinished) or "(children only)",
            )

    # --- the per-app task ------------------------------------------------------------------

    def _spawn(self, slug: str) -> _AppState:
        state = _AppState(slug)
        state.task = asyncio.create_task(self._work(state), name=f"reconcile-{slug}")
        self._states[slug] = state
        return state

    async def _work(self, state: _AppState) -> None:
        while True:
            await state.wake.wait()
            state.wake.clear()
            state.is_busy = True
            try:
                await self._reconcile(state)
            except Exception:
                # The last line of defence of a task that must outlive any one app's failure.
                logger.exception("reconciling %s failed unexpectedly", state.slug)
            finally:
                state.is_busy = False
                if not any(s.is_busy or s.wake.is_set() for s in self._states.values()):
                    self._idle.set()

    async def _watch(self, interval_s: float) -> None:
        seen: RegistrySnapshot | None = None
        while True:
            try:
                snapshot = self._registry.snapshot
            except RegistryUnavailable:
                snapshot = None  # nothing loaded yet: look again at the next tick
            if snapshot is not None:
                is_recovered = self._was_held_back and self._registry.last_error is None
                if snapshot is not seen or is_recovered:
                    seen = snapshot
                    self._was_held_back = False
                    self.wake_all()
            await asyncio.sleep(interval_s)

    # --- one pass over one app -------------------------------------------------------------

    async def _reconcile(self, state: _AppState) -> None:
        slug = state.slug
        if state.stop_waiters:
            # Asked for by a person, not derived from a snapshot, so it does not wait for the
            # registry to be healthy: it only removes what runs.
            await self._stop_on_request(state)
            return
        if state.renamed_to is not None:
            self._hand_over(state)
            return
        if self._registry.last_error is not None:
            self._was_held_back = True
            logger.warning("not reconciling %s while the registry reports an error", slug)
            return
        snapshot = self._registry.snapshot
        app, config = snapshot.app(slug), snapshot.app_config(slug)
        if app is None or config is None or config.desired_state == "stopped":
            await self._take_down(state)
            return
        if not await self._reset_interrupted(snapshot, slug):
            return
        if not await self._keep_running(state, app, config):
            return
        snapshot = self._registry.snapshot
        target = _pending_target(snapshot, config)
        if target is not None:
            await self._run(state, app, config, target, is_restore=False)

    async def _keep_running(self, state: _AppState, app: App, config: AppConfig) -> bool:
        """Make a copy of the active release run; False if the app was stopped while this waited.

        Four cases, in this order: a person asked for a restart; a copy died and its restart is
        due (``_recover``); the restarts are used up (nothing happens); nothing ever ran in this
        process (the start-up restore, whose failure marks the release ``failed``).
        """
        active = _active_release(self._registry.snapshot, state.slug)
        is_restart_asked, state.restart_requested = state.restart_requested, False
        if active is None:
            return True
        if is_restart_asked:
            if state.live is None:
                self._forget_crash(state)
            is_up = await self._run(state, app, config, active, is_restore=True, is_restart=True)
            if not is_up and state.live is None:
                state.is_crashed = True
            return True
        if state.live is not None:
            return True
        if state.needs_restart:
            return await self._recover(state, config, active)
        if state.is_crashed:
            return True
        await self._run(state, app, config, active, is_restore=True)
        return True

    async def _recover(self, state: _AppState, config: AppConfig, release: Release) -> bool:
        """The live copy died: start the same release again after the backoff, or give up.

        Each failed attempt (the process exits or is not healthy in time) counts as the next
        restart. Returns False if the app was stopped or removed during a wait.
        """
        slug = state.slug
        while True:
            delay_s = state.budget.next_delay()
            if delay_s is None:
                state.needs_restart = False
                state.is_crashed = True
                logger.error(
                    "%s is crashed: %d restarts did not keep it up; a person must restart it",
                    slug,
                    state.budget.restarts,
                )
                return True
            logger.warning("restarting %s in %.0f s", _name(release), delay_s)
            await self._clock.sleep(delay_s)
            snapshot = self._registry.snapshot
            app, fresh = snapshot.app(slug), snapshot.app_config(slug)
            if app is None or fresh is None or fresh.desired_state == "stopped":
                await self._take_down(state)
                return False
            state.budget.record_restart()
            if await self._run(state, app, fresh, release, is_restore=True, is_restart=True):
                return True

    def _forget_crash(self, state: _AppState) -> None:
        state.budget.reset()
        state.needs_restart = False
        state.is_crashed = False
        state.crash = None
        state.restart_failure = None

    async def _take_down(self, state: _AppState) -> None:
        """The app is stopped or gone: stop routing to it and stop its child; rows stay as is."""
        self._swap.close(state.slug)
        live, state.live = state.live, None
        state.restart_requested = False
        self._forget_crash(state)
        if live is not None:
            await self._stop_child(live.live.child)

    async def _stop_on_request(self, state: _AppState) -> None:
        """Take the app down for ``stop_app``; a later pass restores what the registry wants."""
        waiters, state.stop_waiters = state.stop_waiters, []
        try:
            await self._take_down(state)
        finally:
            for stopped in waiters:
                stopped.set()

    def _hand_over(self, state: _AppState) -> None:
        """The app was renamed: its running copy moves to the task of the new slug (FR-010).

        The copy is served under the new slug from this line on, and the new task starts a second
        copy under that name beside it and swaps to it, so the rename is a restart through the
        normal swap. If that copy fails, this one keeps serving.
        """
        new_slug, state.renamed_to = state.renamed_to, None
        live, state.live = state.live, None
        self._forget_crash(state)
        state.restart_requested = False
        if new_slug is None:
            return
        successor = self._states.get(new_slug) or self._spawn(new_slug)
        if live is not None:
            if live.watcher is not None:
                live.watcher.cancel()
            moved = _Copy(
                LiveCopy(under_new_slug(live.live.release, new_slug), live.live.child),
                live.budget_mb,
                live.environment,
                live.since_s,
            )
            self._swap.rename_route(state.slug, new_slug, moved.live)
            self._guard(successor, moved)
            successor.live = moved
            successor.restart_requested = True
        self.wake(new_slug)

    async def _reset_interrupted(self, snapshot: RegistrySnapshot, slug: str) -> bool:
        """Send releases left half-way by a restart back to ``pending``; False if that failed.

        This pass is the only writer for the app and none of its releases is in flight now, so a
        ``building``, ``starting`` or ``healthy`` release is a leftover (constitution II).
        """
        stale = [
            release
            for release in snapshot.releases_for(slug)
            if release.status in _INTERRUPTED_STATUSES and release.deleted_at is None
        ]
        if not stale:
            return True
        try:
            await self._registry.write([release_status_change(r, "pending") for r in stale])
        except RegistryError as exc:
            logger.warning("could not reset interrupted releases of %s: %s", slug, exc)
            return False
        return True

    # --- bringing one release up -----------------------------------------------------------

    async def _run(
        self,
        state: _AppState,
        app: App,
        config: AppConfig,
        release: Release,
        *,
        is_restore: bool,
        is_restart: bool = False,
    ) -> bool:
        """Bring ``release`` up and swap to it; True if it is now the live copy."""
        attempt = _Attempt(state, app, config, release, is_restore, is_restart=is_restart)
        replaced: _Copy | None = None
        is_up = False
        try:
            replaced = await self._bring_up(attempt)
            is_up = True
        except _Refused as refusal:
            await self._fail(attempt, refusal.reason, refusal.log_tail)
        except RegistryError as exc:
            # The registry, not the release, failed: no row can be trusted to take a status now.
            # The release stays as it is and the next pass resets it to pending and tries again.
            logger.warning("the registry failed while releasing %s: %s", _name(release), exc)
            if attempt.child is not None:
                await self._stop_child(attempt.child)
        except asyncio.CancelledError:
            await self._interrupted(attempt)
            raise
        except Exception:
            logger.exception("releasing %s failed unexpectedly", _name(release))
            await self._fail(attempt, _INTERNAL_ERROR, "")
        finally:
            state.incoming = None
        if replaced is not None:
            await self._retire(state, replaced)
        return is_up

    async def _bring_up(self, attempt: _Attempt) -> _Copy | None:
        """Run the steps; return the copy this release replaced (to retire), or ``None``."""
        attempt.environment = await self._environment(attempt)
        command = _start_command(attempt.row, attempt.environment)
        await self._advance(attempt, "building")
        built_dir, overlays = await self._build(attempt)
        incoming = self._admit(attempt)
        await self._advance(attempt, "starting")
        child = await self._launch(attempt, incoming, command, built_dir, overlays)
        incoming.started_at_s = self._clock.monotonic()
        await self._wait_until_healthy(attempt, child)
        await self._advance(attempt, "healthy")
        return await self._go_live(attempt, child)

    async def _environment(self, attempt: _Attempt) -> Mapping[str, str]:
        try:
            return dict(
                await self._environments.environment_for(attempt.row, port=self._child_port)
            )
        except EnvironmentRefused as exc:
            raise _Refused(str(exc)) from exc

    async def _advance(self, attempt: _Attempt, new_status: str) -> None:
        """Write the release's next status; a restored release keeps the ``active`` it has."""
        if attempt.is_restore:
            return
        await self._registry.write([release_status_change(attempt.row, new_status)])
        attempt.row = attempt.row.model_copy(
            update={"status": new_status, "status_reason": None, "build_log_tail": None}
        )

    async def _build(self, attempt: _Attempt) -> tuple[Path, tuple[Path, ...]]:
        try:
            built = await self._builder.build(attempt.row)
        except BuildFailed as exc:
            raise _Refused(exc.reason, exc.log_tail) from exc
        return built.release_dir, tuple(built.overlays)

    def _admit(self, attempt: _Attempt) -> _Incoming:
        """Check memory and book it, with no await between the two so nobody else gets in."""
        budget_mb = attempt.app.memory_budget_mb
        decision = check_admission(
            limit_mb=self._memory_limit_mb,
            reserve_mb=self._memory_reserve_mb(),
            running=self._books(),
            candidate_budget_mb=budget_mb,
            now_s=self._clock.monotonic(),
        )
        if not decision.admitted:
            raise _Refused(f"not enough memory: {decision.reason}")
        incoming = _Incoming(budget_mb, attempt.config.health_deadline_s)
        attempt.state.incoming = incoming
        return incoming

    async def _launch(
        self,
        attempt: _Attempt,
        incoming: _Incoming,
        command: tuple[str, ...],
        release_dir: Path,
        overlays: tuple[Path, ...],
    ) -> RunningChild:
        row = attempt.row
        start = ChildStart(
            slug=row.app_slug,
            release_no=row.release_no,
            command=command,
            environment=attempt.environment,
            release_dir=release_dir,
            port=self._child_port,
            memory_budget_mb=attempt.app.memory_budget_mb,
            overlays=overlays,
            app_egress=tuple(attempt.app.egress),
        )
        try:
            child = await self._launcher.start(start)
            attempt.child = incoming.child = child
            await child.wait_started(timeout_s=self._start_timeout_s)
        except ChildStartError as exc:
            raise _Refused(str(exc)) from exc
        return child

    async def _wait_until_healthy(self, attempt: _Attempt, child: RunningChild) -> None:
        """Poll the child; a process that exits first fails the release without waiting."""
        health = asyncio.create_task(
            wait_until_healthy(
                self._probe,
                socket_path=child.socket_path,
                health_path=attempt.config.health_path,
                deadline_s=attempt.config.health_deadline_s,
                clock=self._clock,
            )
        )
        exited = asyncio.create_task(child.wait())
        try:
            await asyncio.wait({health, exited}, return_when=asyncio.FIRST_COMPLETED)
            failure = _health_failure(health, exited)
        finally:
            for task in (health, exited):
                task.cancel()
            await asyncio.gather(health, exited, return_exceptions=True)
        if failure is not None:
            raise _Refused(failure)

    async def _go_live(self, attempt: _Attempt, child: RunningChild) -> _Copy | None:
        """The swap, then record the new copy as the live one; return the copy it replaced."""
        state = attempt.state
        copy = LiveCopy(attempt.row, child)
        if attempt.is_restore:
            self._swap.open_active(copy)
        else:
            await self._swap.activate(copy)
        replaced = state.live
        state.live = _Copy(
            LiveCopy(attempt.row.model_copy(update={"status": "active"}), child),
            attempt.app.memory_budget_mb,
            attempt.environment,
            self._clock.monotonic(),
        )
        self._guard(state, state.live)
        state.incoming = None
        state.needs_restart = False
        state.is_crashed = False
        state.restart_failure = None
        if not attempt.is_restore:
            # A release that went through the swap is a new start for the crash count.
            self._forget_crash(state)
        return replaced

    async def _retire(self, state: _AppState, old: _Copy) -> None:
        """Drain and stop the replaced copy. Its failure never counts against the new release."""
        state.draining.append(old)
        replacement = state.live
        is_same_release = (
            replacement is not None
            and replacement.live.release.release_no == old.live.release.release_no
        )
        try:
            await self._swap.retire(old.live, close_streams=not is_same_release)
        except Exception:
            logger.exception("could not retire %s", _name(old.live.release))
        finally:
            state.draining.remove(old)

    # --- when it does not go up ------------------------------------------------------------

    async def _fail(self, attempt: _Attempt, reason: str, log_tail: str) -> None:
        if attempt.is_restart:
            await self._restart_failed(attempt, reason, log_tail)
        else:
            await self._give_up(attempt, reason, log_tail)

    async def _restart_failed(self, attempt: _Attempt, reason: str, log_tail: str) -> None:
        """A new copy of the running release did not come up: stop it and say why, nothing more.

        The release is not at fault, so its row keeps the status it has and a copy that serves
        keeps serving (FR-004, FR-010); the reason is kept in memory and in the log, because the
        registry's ``status_reason`` belongs to the release.
        """
        child = attempt.child
        if child is not None:
            log_tail = log_tail or child.log_tail()
            await self._stop_child(child)
        values = attempt.environment.values()
        shown = _without_values(reason, values)
        attempt.state.restart_failure = clean_reason(shown)
        logger.warning(
            "a new copy of %s did not come up: %s\n%s",
            _name(attempt.row),
            shown,
            _without_values(log_tail, values),
        )

    async def _give_up(self, attempt: _Attempt, reason: str, log_tail: str) -> None:
        """Stop the child, then store the failure on the release, minus any value the child held."""
        child = attempt.child
        if child is not None:
            log_tail = log_tail or child.log_tail()
            await self._stop_child(child)
        values = attempt.environment.values()
        try:
            await self._registry.write(
                [
                    release_status_change(
                        attempt.row,
                        "failed",
                        reason=_without_values(reason, values),
                        log_tail=_without_values(log_tail, values),
                    )
                ]
            )
        except RegistryError as exc:
            logger.warning(
                "could not record the failure of %s: %s; the next pass resets it",
                _name(attempt.row),
                exc,
            )

    async def _interrupted(self, attempt: _Attempt) -> None:
        """Cancelled mid-way (shutdown): hand the release back as pending.

        Its child is not stopped here: ``stop_all`` cancels the task and stops the child it booked
        in the same breath, so the stop runs once, in parallel with every other app's.
        """
        if attempt.is_restore or attempt.row.status not in _INTERRUPTED_STATUSES:
            return
        try:
            await self._registry.write([release_status_change(attempt.row, "pending")])
        except RegistryError as exc:
            logger.warning("could not hand %s back as pending: %s", _name(attempt.row), exc)

    # --- a copy that dies -------------------------------------------------------------------

    def _guard(self, state: _AppState, copy: _Copy) -> None:
        """Watch ``copy``'s child: if it exits while it is the live copy, that is a crash."""
        copy.watcher = asyncio.create_task(self._on_exit(state, copy), name=f"exit-{state.slug}")

    async def _on_exit(self, state: _AppState, copy: _Copy) -> None:
        ended = await copy.live.child.wait()
        if state.live is not copy or self._is_stopping:
            return  # this reconciler ended it: replaced, stopped, renamed or shutting down
        values = copy.environment.values()
        report = CrashReport(
            release_no=copy.live.release.release_no,
            exit_description=_without_values(ended.description, values),
            log_tail=_without_values(clean_log_tail(copy.live.child.log_tail()) or "", values),
        )
        self._swap.close(state.slug)
        state.live = None
        state.crash = report
        state.budget.note_crash(ran_for_s=self._clock.monotonic() - copy.since_s)
        state.needs_restart = True
        logger.warning(
            "%s v%d died (%s) and its route is closed; last log lines:\n%s",
            state.slug,
            report.release_no,
            report.exit_description,
            report.log_tail or "(none)",
        )
        self.wake(state.slug)

    async def _stop_child(self, child: RunningChild) -> None:
        try:
            await asyncio.shield(child.stop(grace_s=self._stop_grace_s))
        except Exception:
            # Boundary: a child that cannot be stopped must not stop the release from being
            # recorded; the container's exit ends it.
            logger.exception("could not stop a child")

    async def _stop_with_grace(self, child: RunningChild, grace_s: float) -> None:
        try:
            await child.stop(grace_s=grace_s)
        except Exception:
            logger.exception("could not stop a child at shutdown")

    # --- the books -------------------------------------------------------------------------

    def _books(self) -> list[RunningApp]:
        """What runs now, as admission control counts it (the overlap and the drain included)."""
        now_s = self._clock.monotonic()
        books: list[RunningApp] = []
        for state in self._states.values():
            incoming = state.incoming
            if state.live is not None:
                second = None
                if incoming is not None:
                    started_s = (
                        incoming.started_at_s if incoming.started_at_s is not None else now_s
                    )
                    second = SecondCopy(incoming.budget_mb, started_s, incoming.health_deadline_s)
                books.append(RunningApp(state.slug, state.live.budget_mb, second))
            elif incoming is not None:
                books.append(RunningApp(state.slug, incoming.budget_mb))
            books.extend(
                RunningApp(f"{state.slug}~draining-{number}", copy.budget_mb)
                for number, copy in enumerate(state.draining)
            )
        return books

    def _tracked_children(self) -> list[RunningChild]:
        children: list[RunningChild] = []
        for state in self._states.values():
            if state.live is not None:
                children.append(state.live.live.child)
            children.extend(copy.live.child for copy in state.draining)
            if state.incoming is not None and state.incoming.child is not None:
                children.append(state.incoming.child)
        return children


def _active_release(snapshot: RegistrySnapshot, slug: str) -> Release | None:
    return next(
        (
            release
            for release in snapshot.releases_for(slug)
            if release.status == "active" and release.deleted_at is None
        ),
        None,
    )


def _pending_target(snapshot: RegistrySnapshot, config: AppConfig) -> Release | None:
    if config.target_release_no is None:
        return None
    release = snapshot.release(config.app_slug, config.target_release_no)
    if release is None or release.status != "pending" or release.deleted_at is not None:
        return None
    return release


def _start_command(release: Release, environment: Mapping[str, str]) -> tuple[str, ...]:
    command = list(release.detected.command)
    if not command:
        default = _DEFAULT_COMMANDS.get(release.detected.runtime or "")
        if default is None:
            raise _Refused(_NO_COMMAND)
        command = list(default)
    return tuple(substitute_command_arguments(command, environment))


def _health_failure(
    health: asyncio.Task[HealthResult], exited: asyncio.Task[ChildExit]
) -> str | None:
    """Why the release is not healthy, or ``None`` if the first answer came before any exit."""
    result = health.result() if health.done() and not health.cancelled() else None
    if result is not None and result.is_healthy:
        return None
    if exited.done() and not exited.cancelled():
        return f"the process {exited.result().description} before the health check passed"
    return (result.reason if result is not None else None) or "the health check did not finish"


def _without_values(text: str, values: Iterable[str]) -> str:
    """``text`` with each long-enough value of the child's environment replaced by ``***``."""
    hidden = sorted(
        {v for v in values if len(v) >= MIN_REDACTED_VALUE_CHARS}, key=len, reverse=True
    )
    for value in hidden:
        text = text.replace(value, _REDACTED)
    return text


def _name(release: Release) -> str:
    return f"{release.app_slug} v{release.release_no}"
