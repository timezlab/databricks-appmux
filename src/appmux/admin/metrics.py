"""``GET /audit``, ``GET /metrics`` and ``GET /metrics/breakdown`` (T169, T193, design 10.2).

All three are reads: they change no row and record no audit entry, and each is bounded so that a
request cannot ask the registry for an unbounded amount.

``GET /audit?app=&from=&to=&limit=`` (``platform.view``) lists audit rows, newest first, in the
half-open window ``from <= ts < to``. ``to`` defaults to now and ``from`` to 24 hours before
``to``; ``limit`` defaults to 100 and is at most 1000. There is no cursor: to read further back a
client sends the ``ts`` of the last row as the next ``to``. A row is shown only if the audit model
would still accept it as a new entry. A row that fails that (text shaped like a credential, which a
manual change made in the database can hold, or a key the writer never produces) is shown without
its detail and with a fixed reason in its place, so the fact that it happened stays visible and the
text never leaves the server.

``GET /metrics?app=&from=&to=&step=`` answers with one row per series and step. ``app`` is a slug
(``app.view``, ``CAN_MANAGE`` on the app), or ``_platform`` for the platform row, or absent for
every series (``platform.view``). ``to`` defaults to now and ``from`` to an hour before ``to``.
``step`` is ``1m`` (the default: the stored minute rows as they are), ``5m``, ``15m`` or ``1h``,
the minutes added up by the rules of ``registry/metric_steps.py``. The window is at most 7 days
and at most ``MAX_METRIC_STEPS`` steps (its length divided by the step, rounded up); a longer one
is ``400 window_too_long``, and the message names the smallest step that fits. The answer holds at
most ``MAX_METRIC_ROWS`` rows: when the window would hold more, whole steps are dropped oldest
first (so every series keeps the same span), ``from`` moves to the start of the oldest step kept
and ``truncated`` is ``true``. The registry is asked for one row more than the cap; the extra row
only says "there are more", and because the registry drops the oldest rows first it also means
the oldest step it returned may hold only some of its series, so that step is dropped whole.

``GET /metrics/breakdown?from=&to=&step=&limit=`` (``platform.view``) answers with the apps that
served the most requests in the window, each a series of points per step, and the rest added
together as ``other`` (``null`` when there is no rest). The window and the step follow the rules
of ``GET /metrics``; ``limit`` is how many apps get a series of their own, 8 by default and at most
``MAX_BREAKDOWN_LIMIT``. A breakdown is one point per step and series, so it needs no row cap: 21
series of at most 1440 steps.

A query that is not valid is ``400`` and the message names the parameter, never the value sent.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Final

from pydantic import TypeAdapter, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from appmux.admin.api import AdminContext
from appmux.admin.errors import BadRequest
from appmux.admin.schemas import (
    AuditList,
    AuditRow,
    MetricsBreakdown,
    MetricsPoint,
    MetricsResponse,
    MetricsRow,
    MetricsSeries,
    MetricStep,
)
from appmux.authz.actions import Action
from appmux.registry.audit import AUDIT_ACTIONS, AuditEntry, SafeActor, SafeSlug
from appmux.registry.metric_steps import BREAKDOWN_COLUMNS, MetricBreakdownRow
from appmux.registry.models import PLATFORM_METRICS_SLUG, SLUG_PATTERN, AuditLogRow, MetricRow
from appmux.registry.snapshot import RegistrySnapshot

MAX_METRIC_ROWS: Final = 10_000
# Smallest first: the step a too-long window is pointed to is the first one that fits.
METRIC_STEPS: Final[Mapping[MetricStep, timedelta]] = MappingProxyType(
    {
        "1m": timedelta(minutes=1),
        "5m": timedelta(minutes=5),
        "15m": timedelta(minutes=15),
        "1h": timedelta(hours=1),
    }
)
DEFAULT_METRIC_STEP: Final[MetricStep] = "1m"
MAX_METRIC_STEPS: Final = 1440
MAX_METRIC_WINDOW: Final = timedelta(days=7)
DEFAULT_METRIC_WINDOW: Final = timedelta(hours=1)
DEFAULT_BREAKDOWN_LIMIT: Final = 8
MAX_BREAKDOWN_LIMIT: Final = 20
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


def _limit(request: Request, *, default: int, maximum: int) -> int:
    raw = _single(request, "limit")
    if raw is None:
        return default
    if (
        len(raw) <= _MAX_LIMIT_DIGITS
        and raw.isascii()
        and raw.isdecimal()
        and 1 <= int(raw) <= maximum
    ):
        return int(raw)
    raise BadRequest(f"limit must be a whole number from 1 to {maximum}")


def _step(request: Request) -> MetricStep:
    raw = _single(request, "step")
    if raw is None:
        return DEFAULT_METRIC_STEP
    for name in METRIC_STEPS:
        if raw == name:
            return name
    raise BadRequest(f"step must be one of {', '.join(METRIC_STEPS)}")


def _step_count(span: timedelta, step: MetricStep) -> int:
    return math.ceil(span / METRIC_STEPS[step])


def _require_window_fits(start: datetime, end: datetime, step: MetricStep) -> None:
    """Refuse a window of more than 7 days, or of more than ``MAX_METRIC_STEPS`` steps."""
    span = end - start
    if span > MAX_METRIC_WINDOW:
        raise BadRequest(
            "the window is longer than 7 days, at every step; ask for a shorter one",
            code="window_too_long",
        )
    if _step_count(span, step) <= MAX_METRIC_STEPS:
        return
    # Within 7 days the longest step always fits, so there is a step to name.
    fitting = next(name for name in METRIC_STEPS if _step_count(span, name) <= MAX_METRIC_STEPS)
    raise BadRequest(
        f"the window holds more than {MAX_METRIC_STEPS} steps of {step}; "
        f"ask with step={fitting} or for a shorter window",
        code="window_too_long",
    )


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


def metrics_point(row: MetricBreakdownRow) -> MetricsPoint:
    """The contract's ``MetricsPoint`` for one step of a breakdown series."""
    values = {name: getattr(row, name) for name in ("minute", *BREAKDOWN_COLUMNS)}
    return MetricsPoint.model_validate(values)


def breakdown_series(
    rows: Sequence[MetricBreakdownRow], snapshot: RegistrySnapshot
) -> tuple[list[MetricsSeries], list[MetricsPoint] | None]:
    """``(series, other)`` from the registry's rows, which come series by series in rank order."""
    points_by_app: dict[str, list[MetricsPoint]] = {}
    other: list[MetricsPoint] = []
    for row in rows:
        point = metrics_point(row)
        if row.app_slug is None:
            other.append(point)
        else:
            points_by_app.setdefault(row.app_slug, []).append(point)
    series = []
    for slug, points in points_by_app.items():
        app = snapshot.app(slug)
        series.append(
            MetricsSeries(
                app_slug=slug,
                display_name=app.display_name if app is not None else None,
                points=points,
            )
        )
    # The rest has a row for each of its steps, so no row means there is no rest.
    return series, other or None


class MetricsRoutes:
    """The route provider of ``GET /audit``, ``GET /metrics`` and ``GET /metrics/breakdown``.

    ``clock`` supplies "now" for the default windows. ``max_rows`` is the cap on one
    ``GET /metrics`` answer (``MAX_METRIC_ROWS``); a smaller one is for tests.
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
            limit = _limit(request, default=DEFAULT_AUDIT_LIMIT, maximum=MAX_AUDIT_LIMIT)
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
            step = _step(request)
            _require_window_fits(start, end, step)
            if step == DEFAULT_METRIC_STEP:
                rows = await context.registry.read_metrics(
                    app_slug=app_slug, start=start, end=end, limit=self._max_rows + 1
                )
            else:
                rows = await context.registry.read_metric_steps(
                    app_slug=app_slug,
                    start=start,
                    end=end,
                    step=METRIC_STEPS[step],
                    limit=self._max_rows + 1,
                )
            # A step row carries its step's start as its minute, so whole steps are whole minutes.
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

        async def get_metrics_breakdown(request: Request) -> Response:
            await context.require(request, Action.PLATFORM_VIEW)
            start, end = _window(request, now=self._clock(), default_span=DEFAULT_METRIC_WINDOW)
            step = _step(request)
            _require_window_fits(start, end, step)
            top = _limit(request, default=DEFAULT_BREAKDOWN_LIMIT, maximum=MAX_BREAKDOWN_LIMIT)
            rows = await context.registry.read_metric_breakdown(
                start=start, end=end, step=METRIC_STEPS[step], top=top
            )
            series, other = breakdown_series(rows, context.registry.snapshot)
            body = MetricsBreakdown(from_=start, to=end, step=step, series=series, other=other)
            return JSONResponse(body.model_dump(mode="json"))

        return [
            Route("/audit", get_audit, methods=["GET"]),
            Route("/metrics", get_metrics, methods=["GET"]),
            Route("/metrics/breakdown", get_metrics_breakdown, methods=["GET"]),
        ]
