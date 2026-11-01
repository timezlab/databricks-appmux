"""``GET /audit`` and ``GET /metrics`` (T169, FR-049, design 10.2).

Both are reads: they change no row and record no audit entry, and both are bounded so that a
request cannot ask the registry for an unbounded amount.

``GET /audit?app=&from=&to=&limit=`` (``platform.view``) lists audit rows, newest first, in the
half-open window ``from <= ts < to``. ``to`` defaults to now and ``from`` to 24 hours before
``to``; ``limit`` defaults to 100 and is at most 1000. There is no cursor: to read further back a
client sends the ``ts`` of the last row as the next ``to``. A row is shown only if the audit model
would still accept it as a new entry. A row that fails that (text shaped like a credential, which a
manual change made in the database can hold, or a key the writer never produces) is shown without
its detail and with a fixed reason in its place, so the fact that it happened stays visible and the
text never leaves the server.

``GET /metrics?app=&from=&to=`` answers with one-minute rows. ``app`` is a slug (``app.view``,
``CAN_MANAGE`` on the app), or ``_platform`` for the platform row, or absent for every series
(``platform.view``). ``to`` defaults to now and ``from`` to an hour before ``to``; a window longer
than 1440 minutes is ``400``. The answer holds at most ``MAX_METRIC_ROWS`` rows: when the window
would hold more, whole minutes are dropped oldest first (so every series keeps the same span),
``from`` moves to the start of the oldest minute kept and ``truncated`` is ``true``. The registry
is asked for one row more than the cap; the extra row only says "there are more", and because the
registry drops the oldest rows first it also means the oldest minute it returned may hold only
some of its series, so that minute is dropped whole.

A query that is not valid is ``400`` and the message names the parameter, never the value sent.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Final

from pydantic import TypeAdapter, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from appmux.admin.api import AdminContext
from appmux.admin.errors import BadRequest
from appmux.admin.schemas import AuditList, AuditRow, MetricsResponse, MetricsRow
from appmux.authz.actions import Action
from appmux.registry.audit import AUDIT_ACTIONS, AuditEntry, SafeActor, SafeSlug
from appmux.registry.models import PLATFORM_METRICS_SLUG, SLUG_PATTERN, AuditLogRow, MetricRow

MAX_METRIC_ROWS: Final = 10_000
MAX_METRIC_WINDOW_MINUTES: Final = 1440
DEFAULT_METRIC_WINDOW: Final = timedelta(hours=1)
DEFAULT_AUDIT_WINDOW: Final = timedelta(hours=24)
DEFAULT_AUDIT_LIMIT: Final = 100
MAX_AUDIT_LIMIT: Final = 1000

# What a client sees in place of a row's text that cannot be shown.
WITHHELD: Final = "withheld"
WITHHELD_REASON: Final = (
    "the stored text of this row looks like a credential or is not in a form appmux records, so it "
    "is not shown; read the row in the database if you need it"
)

_MAX_LIMIT_DIGITS: Final = 6
_TIME_HINT: Final = (
    "a time with a zone in ISO 8601 form (year-month-day, 'T', hour:minute:second, 'Z')"
)
_SLUG_SHAPE: Final = re.compile(SLUG_PATTERN)
_ACTOR: Final = TypeAdapter(SafeActor)
_SLUG: Final = TypeAdapter(SafeSlug)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _single(request: Request, name: str) -> str | None:
    values = request.query_params.getlist(name)
    if len(values) > 1:
        raise BadRequest(f"{name} may be sent once")
    return values[0] if values else None


def _time(request: Request, name: str) -> datetime | None:
    raw = _single(request, name)
    if raw is None:
        return None
    try:
        moment = datetime.fromisoformat(raw)
        if moment.utcoffset() is None:
            raise ValueError(name)  # a time with no zone is a guess, not an instant
        return moment.astimezone(UTC)
    except (ValueError, OverflowError):
        raise BadRequest(f"{name} is not a time: send {_TIME_HINT}") from None


def _window(
    request: Request, *, now: datetime, default_span: timedelta
) -> tuple[datetime, datetime]:
    """``(from, to)``: ``to`` defaults to now, ``from`` to ``default_span`` before ``to``."""
    start, end = _time(request, "from"), _time(request, "to")
    end = end if end is not None else now
    start = start if start is not None else end - default_span
    if start >= end:
        raise BadRequest("from must be earlier than to")
    return start, end


def _slug_of(request: Request, *, allow_platform_row: bool) -> str | None:
    raw = _single(request, "app")
    if raw is None:
        return None
    is_platform_row = allow_platform_row and raw == PLATFORM_METRICS_SLUG
    if not is_platform_row and _SLUG_SHAPE.fullmatch(raw) is None:
        raise BadRequest("app is not an app slug (lower-case letters, digits and '-')")
    return raw


def _audit_limit(request: Request) -> int:
    raw = _single(request, "limit")
    if raw is None:
        return DEFAULT_AUDIT_LIMIT
    if (
        len(raw) <= _MAX_LIMIT_DIGITS
        and raw.isascii()
        and raw.isdecimal()
        and 1 <= int(raw) <= MAX_AUDIT_LIMIT
    ):
        return int(raw)
    raise BadRequest(f"limit must be a whole number from 1 to {MAX_AUDIT_LIMIT}")


def audit_row(row: AuditLogRow) -> AuditRow:
    """The contract's ``AuditRow``, or the row without its text when the text cannot be shown."""
    try:
        AuditEntry.model_validate(
            {
                "actor": row.actor,
                "action": row.action,
                "app_slug": row.app_slug,
                "detail": row.detail,
                "reason": row.reason,
                "automatic": row.detail.get("automatic") is True,
            }
        )
    except ValidationError:
        return _withheld(row)
    return AuditRow(
        id=row.id,
        ts=row.ts,
        actor=row.actor,
        action=row.action,
        app_slug=row.app_slug,
        detail=dict(row.detail),
        reason=row.reason,
    )


def _withheld(row: AuditLogRow) -> AuditRow:
    """What stays of a row whose text is not shown: when, which of the known names, and that."""
    return AuditRow(
        id=row.id,
        ts=row.ts,
        actor=row.actor if _valid(_ACTOR, row.actor) else WITHHELD,
        action=row.action if row.action in AUDIT_ACTIONS else WITHHELD,
        app_slug=row.app_slug if row.app_slug is None or _valid(_SLUG, row.app_slug) else WITHHELD,
        detail={},
        reason=WITHHELD_REASON,
    )


def _valid(adapter: TypeAdapter[str], text: str) -> bool:
    try:
        adapter.validate_python(text)
    except ValidationError:
        return False
    return True


def metrics_row(row: MetricRow) -> MetricsRow:
    """The contract's ``MetricsRow`` for a stored row (the columns have the same names)."""
    values = {name: getattr(row, name) for name in MetricsRow.model_fields}
    values["egress_dropped_by_rule"] = dict(row.egress_dropped_by_rule.root)
    return MetricsRow.model_validate(values)


def whole_minutes(rows: Sequence[MetricRow], *, limit: int) -> tuple[list[MetricRow], bool]:
    """``rows`` (oldest minute first, at most ``limit + 1`` of them) cut to whole minutes.

    ``(kept, truncated)``: more than ``limit`` rows means the registry had more than it was asked
    to return, so the oldest minute in hand is dropped whole, since it may be only part of itself.
    """
    if len(rows) <= limit:
        return list(rows), False
    oldest = rows[0].minute
    return [row for row in rows if row.minute != oldest], True


class MetricsRoutes:
    """The route provider of ``GET /audit`` and ``GET /metrics``.

    ``clock`` supplies "now" for the default windows. ``max_rows`` is the cap on one metrics answer
    (``MAX_METRIC_ROWS``); a smaller one is for tests.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = _utc_now,
        max_rows: int = MAX_METRIC_ROWS,
    ) -> None:
        if max_rows < 1:
            raise ValueError("max_rows must be at least 1")
        self._clock = clock
        self._max_rows = max_rows

    def routes(self, context: AdminContext) -> Sequence[Route]:
        async def get_audit(request: Request) -> Response:
            await context.require(request, Action.PLATFORM_VIEW)
            app_slug = _slug_of(request, allow_platform_row=False)
            start, end = _window(request, now=self._clock(), default_span=DEFAULT_AUDIT_WINDOW)
            limit = _audit_limit(request)
            rows = await context.registry.read_audit(
                app_slug=app_slug, start=start, end=end, limit=limit
            )
            body = AuditList(from_=start, to=end, items=[audit_row(row) for row in rows])
            return JSONResponse(body.model_dump(mode="json"))

        async def get_metrics(request: Request) -> Response:
            # The app decides which right is needed, so it is read first; a malformed one is the
            # same 400 for everybody. The rest of the query is read after the right is known.
            app_slug = _slug_of(request, allow_platform_row=True)
            if app_slug is None or app_slug == PLATFORM_METRICS_SLUG:
                await context.require(request, Action.PLATFORM_VIEW)
            else:
                await context.require(request, Action.APP_VIEW, app_slug)
            start, end = _window(request, now=self._clock(), default_span=DEFAULT_METRIC_WINDOW)
            if end - start > timedelta(minutes=MAX_METRIC_WINDOW_MINUTES):
                raise BadRequest(
                    f"the window is longer than {MAX_METRIC_WINDOW_MINUTES} minutes; "
                    "ask for a shorter one"
                )
            rows = await context.registry.read_metrics(
                app_slug=app_slug, start=start, end=end, limit=self._max_rows + 1
            )
            kept, truncated = whole_minutes(rows, limit=self._max_rows)
            if truncated:
                start = kept[0].minute if kept else end
            body = MetricsResponse(
                from_=start,
                to=end,
                truncated=truncated,
                items=[metrics_row(row) for row in kept],
            )
            return JSONResponse(body.model_dump(mode="json"))

        return [
            Route("/audit", get_audit, methods=["GET"]),
            Route("/metrics", get_metrics, methods=["GET"]),
        ]
