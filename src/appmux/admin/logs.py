"""``GET /apps/{app}/logs``: the last lines of an app's output, for its app admins (T189).

The lines come from the app's output ring (``reconciler/output.py``), which the reconciler fills
while the child runs and keeps across a restart and a failed start; they were masked when they were
kept. The route only reads: ``after`` is the last ``seq`` the client has (0 for all) and ``limit``
how many of the newest lines after it to return.

A page is polled every few seconds, so a read writes no audit row, except the first read of an app
by an actor in ``VIEW_AUDIT_WINDOW``: that one writes an ``app.view_logs`` row with no content, so
the log shows who looked at an app's output and when, without one row per poll. The memory of who
read what is this process's: a restart of appmux makes the next read of each person audited again.
If the row cannot be written, the read fails and the next one tries again.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from appmux.admin.api import AdminContext
from appmux.admin.errors import BadRequest
from appmux.admin.schemas import AppLogExit, AppLogLine, AppLogs
from appmux.authz.actions import Action
from appmux.reconciler.app_control import RunState
from appmux.reconciler.output import OutputRing

DEFAULT_LOG_LIMIT: Final = 500
MAX_LOG_LIMIT: Final = 1000
VIEW_AUDIT_WINDOW: Final = timedelta(minutes=10)
# More digits than any ring can count: a longer number is refused before ``int`` reads it.
_MAX_NUMBER_DIGITS: Final = 12


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AppOutput(Protocol):
    """What the reconciler answers about an app's output (``Reconciler`` and ``Service``)."""

    def run_state(self, slug: str, desired_state: str) -> RunState: ...

    def output(self, slug: str) -> OutputRing | None:
        """The app's ring, or ``None`` when no copy of it was started by this process."""
        ...


class LogRoutes:
    """The route provider of ``GET /apps/{app}/logs``."""

    def __init__(self, apps: AppOutput, *, now: Callable[[], datetime] = _utc_now) -> None:
        self._apps = apps
        self._now = now
        # (actor, slug) -> when that actor's last audited read of the app was.
        self._audited_at: dict[tuple[str, str], datetime] = {}

    def routes(self, context: AdminContext) -> Sequence[Route]:
        async def read_logs(request: Request) -> Response:
            slug = request.path_params["app"]
            await context.require(request, Action.APP_VIEW_LOGS, slug)
            after = _whole_number(request, "after", default=0, low=0, high=None)
            limit = _whole_number(
                request, "limit", default=DEFAULT_LOG_LIMIT, low=1, high=MAX_LOG_LIMIT
            )
            await self._audit_first_read(context, request, slug)
            ring = self._apps.output(slug)
            if ring is None:
                body = AppLogs(
                    state="never_started",
                    instance=None,
                    lines=[],
                    next_after=0,
                    dropped=0,
                    last_exit=None,
                )
                return JSONResponse(body.model_dump(mode="json"))
            config = context.registry.snapshot.app_config(slug)
            desired_state = config.desired_state if config is not None else "stopped"
            page = ring.read(after=after, limit=limit)
            last_exit = page.last_exit
            body = AppLogs(
                state=self._apps.run_state(slug, desired_state),
                instance=page.instance,
                lines=[
                    AppLogLine(seq=line.seq, at=line.at, stream=line.stream, text=line.text)
                    for line in page.lines
                ],
                next_after=page.next_after,
                dropped=page.dropped,
                last_exit=None
                if last_exit is None
                else AppLogExit(
                    code=last_exit.code,
                    signal=last_exit.signal,
                    at=last_exit.at,
                    ran_for_s=last_exit.ran_for_s,
                ),
            )
            return JSONResponse(body.model_dump(mode="json"))

        return [Route("/apps/{app}/logs", read_logs, methods=["GET"])]

    async def _audit_first_read(self, context: AdminContext, request: Request, slug: str) -> None:
        now = self._now()
        self._forget_before(now - VIEW_AUDIT_WINDOW)
        actor = context.actor(request).email
        key = (actor, slug)
        if key in self._audited_at:
            return
        entry = context.audit_entry(request, "app.view_logs", app_slug=slug)
        # Marked before the write, so two reads at once write one row; unmarked if it fails.
        self._audited_at[key] = now
        try:
            await context.write(request, [], [entry])
        except BaseException:
            del self._audited_at[key]
            raise

    def _forget_before(self, cutoff: datetime) -> None:
        """Drop the reads older than the window, so the memory holds only the last 10 minutes."""
        for key in [key for key, at in self._audited_at.items() if at <= cutoff]:
            del self._audited_at[key]


def _whole_number(request: Request, name: str, *, default: int, low: int, high: int | None) -> int:
    values = request.query_params.getlist(name)
    if not values:
        return default
    raw = values[0]
    is_number = (
        len(values) == 1 and len(raw) <= _MAX_NUMBER_DIGITS and raw.isascii() and raw.isdecimal()
    )
    if is_number and low <= int(raw) and (high is None or int(raw) <= high):
        return int(raw)
    upper = "" if high is None else f" to {high}"
    raise BadRequest(f"{name} must be sent once, as a whole number from {low}{upper}")
