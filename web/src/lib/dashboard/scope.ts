/**
 * What the dashboard and an app's Insights ask the metrics API for: the time
 * ranges a person can choose, the step each range is read at, the window sent
 * as `from`/`to` (and the one before it, for the change figures), and how often
 * to ask. No React and no fetch here, so the rules test without a browser.
 *
 * The contract (admin-api.md, `GET /metrics` with `step` and
 * `GET /metrics/breakdown`): a window is at most 7 days and at most 1440 steps
 * (its length divided by the step, rounded up), else `400 window_too_long`;
 * the data is kept for 7 days (data model 8).
 */
import { ApiError } from "@/lib/api";
import type { AppSummary, MetricStep } from "@/lib/api/schemas";
import { isValidSlug } from "@/lib/slug-link";
import { MIN_POLL_INTERVAL_MS } from "@/lib/poll/schedule";
import { parseTime } from "@/lib/format";

/** `app_slug` of the platform-wide row (data model 4.8). Not a valid app slug: the pattern has no underscore. */
export const PLATFORM_APP_SLUG = "_platform";

const MINUTE_MS = 60_000;
const DAY_MINUTES = 1440;

/** Most steps one answer may cover; more is a `400 window_too_long`. */
export const MAX_WINDOW_STEPS = 1440;
/** Longest window the API accepts, and how long the data is kept. */
export const MAX_WINDOW_MINUTES = 7 * DAY_MINUTES;
const RETENTION_MINUTES = MAX_WINDOW_MINUTES;

/** How many apps the breakdown names; the rest add up to its "other" series. */
export const BREAKDOWN_LIMIT = 8;

// ----------------------------------------------------------------- steps

export interface StepChoice {
  id: MetricStep;
  minutes: number;
}

/** The steps of the contract, finest first. */
const STEPS = [
  { id: "1m", minutes: 1 },
  { id: "5m", minutes: 5 },
  { id: "15m", minutes: 15 },
  { id: "1h", minutes: 60 },
] as const satisfies readonly StepChoice[];

/**
 * The finest step that keeps a window of `minutes` within the 1440 steps one
 * answer may hold: 1 minute up to a day, 5 minutes up to 5 days, 15 minutes up
 * to the 7 days the data is kept. A finer step would be refused; a coarser one
 * would blur what the charts can still draw.
 */
export function stepForRange(minutes: number): StepChoice {
  if (
    !Number.isInteger(minutes) ||
    minutes < 1 ||
    minutes > MAX_WINDOW_MINUTES
  ) {
    throw new RangeError(
      `stepForRange: ${minutes} minutes is outside 1 to ${MAX_WINDOW_MINUTES}`,
    );
  }
  const step = STEPS.find(
    (choice) => Math.ceil(minutes / choice.minutes) <= MAX_WINDOW_STEPS,
  );
  // Unreachable: 7 days at the coarsest step is 168 steps.
  if (step === undefined) throw new RangeError("stepForRange: no step fits");
  return step;
}

/** The length of a step of the contract, in minutes: "15m" is 15. */
export function stepMinutes(id: MetricStep): number {
  const step = STEPS.find((choice) => choice.id === id);
  // Unreachable: STEPS lists every step of the contract.
  if (step === undefined) throw new RangeError(`stepMinutes: no step ${id}`);
  return step.minutes;
}

// ---------------------------------------------------------------- ranges

export interface TimeRange {
  id: string;
  /** "Last 6 hours": the name in a menu and in the empty state. */
  label: string;
  /** "6h": the text of the toggle. */
  shortLabel: string;
  /** "6 hours" as in "the previous 6 hours". */
  span: string;
  minutes: number;
}

/** The choices, shortest first, up to the 7 days the data is kept. */
export const TIME_RANGES = [
  {
    id: "15m",
    label: "Last 15 minutes",
    shortLabel: "15m",
    span: "15 minutes",
    minutes: 15,
  },
  { id: "1h", label: "Last hour", shortLabel: "1h", span: "hour", minutes: 60 },
  {
    id: "6h",
    label: "Last 6 hours",
    shortLabel: "6h",
    span: "6 hours",
    minutes: 360,
  },
  {
    id: "24h",
    label: "Last 24 hours",
    shortLabel: "24h",
    span: "24 hours",
    minutes: DAY_MINUTES,
  },
  {
    id: "3d",
    label: "Last 3 days",
    shortLabel: "3d",
    span: "3 days",
    minutes: 3 * DAY_MINUTES,
  },
  {
    id: "7d",
    label: "Last 7 days",
    shortLabel: "7d",
    span: "7 days",
    minutes: 7 * DAY_MINUTES,
  },
] as const satisfies readonly TimeRange[];

export type TimeRangeId = (typeof TIME_RANGES)[number]["id"];

export const DEFAULT_RANGE_ID: TimeRangeId = "1h";

export function isTimeRangeId(value: unknown): value is TimeRangeId {
  return TIME_RANGES.some((range) => range.id === value);
}

/** The range with this id, or `null` (a control's value is a string, not yet a known id). */
export function timeRangeById(id: string): TimeRange | null {
  return TIME_RANGES.find((range) => range.id === id) ?? null;
}

/** The next longer range, offered when a window holds no traffic; `null` after the longest. */
export function longerRange(id: string): (typeof TIME_RANGES)[number] | null {
  const index = TIME_RANGES.findIndex((range) => range.id === id);
  if (index < 0) return null;
  return TIME_RANGES[index + 1] ?? null;
}

// ---------------------------------------------------------------- window

export interface MetricsWindow {
  /** ISO 8601 UTC, whole seconds, `Z`: the form of the contract's examples. */
  from: string;
  to: string;
}

function toQueryTime(ms: number): string {
  return new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z");
}

/**
 * The `minutes` before the start of the current minute. The current minute is
 * left out because its row is written only after it ends (one flush per minute,
 * design 10.2). Both ends come from the browser clock, so the length is exactly
 * `minutes` even when that clock is off; only the position shifts. With a step
 * longer than a minute the first and the last step are cut by the window (the
 * contract keeps a cut step's start).
 */
export function metricsWindow(nowMs: number, minutes: number): MetricsWindow {
  if (!Number.isFinite(nowMs)) {
    throw new RangeError("metricsWindow: the current time is not a number");
  }
  if (
    !Number.isInteger(minutes) ||
    minutes < 1 ||
    minutes > MAX_WINDOW_MINUTES
  ) {
    throw new RangeError(
      `metricsWindow: ${minutes} minutes is outside 1 to ${MAX_WINDOW_MINUTES}`,
    );
  }
  const toMs = Math.floor(nowMs / MINUTE_MS) * MINUTE_MS;
  return {
    from: toQueryTime(toMs - minutes * MINUTE_MS),
    to: toQueryTime(toMs),
  };
}

/**
 * The window of the same length just before `window`, for "12% more than the
 * previous hour". `null` when it would reach further back than the data is
 * kept: those minutes are gone, and a figure against them would read as a
 * rise from nothing.
 */
export function previousWindow(window: MetricsWindow): MetricsWindow | null {
  const fromMs = parseTime(window.from);
  const toMs = parseTime(window.to);
  if (fromMs === null || toMs === null || toMs <= fromMs) return null;
  const startMs = fromMs - (toMs - fromMs);
  if (toMs - startMs > RETENTION_MINUTES * MINUTE_MS) return null;
  return { from: toQueryTime(startMs), to: toQueryTime(fromMs) };
}

/**
 * The server's answer to a window longer than 7 days or than 1440 steps. The
 * ranges here never ask for one, so it means the server's limits and this page's
 * differ (a newer or an older appmux): the page says so, not "not accepted".
 */
export function isWindowTooLong(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    error.status === 400 &&
    error.code === "window_too_long"
  );
}

// ----------------------------------------------------------------- scope

/** The platform view (the `_platform` row and the per-app breakdown) or one app's Insights. */
export type DashboardScope =
  { kind: "platform" } | { kind: "app"; slug: string };

/** The query of `getMetrics` for a scope, a window and a step. */
export function metricsQueryFor(
  scope: DashboardScope,
  window: MetricsWindow,
  step: MetricStep,
): { app: string; from: string; to: string; step: MetricStep } {
  const app = scope.kind === "platform" ? PLATFORM_APP_SLUG : scope.slug;
  return { app, ...window, step };
}

/** The query of `getMetricsBreakdown`: the same window and step, the top apps. */
export function breakdownQueryFor(
  window: MetricsWindow,
  step: MetricStep,
): { from: string; to: string; step: MetricStep; limit: number } {
  return { ...window, step, limit: BREAKDOWN_LIMIT };
}

/**
 * The apps whose Insights someone may read (`CAN_MANAGE`), by slug: what the
 * dashboard lists for a person who is not a platform admin. `display_name` is
 * text from the API: a label, never markup.
 */
export function managedApps(apps: readonly AppSummary[]): AppSummary[] {
  return apps
    .filter(
      (item) => isValidSlug(item.slug) && item.permission === "CAN_MANAGE",
    )
    .toSorted((a, b) => (a.slug < b.slug ? -1 : a.slug > b.slug ? 1 : 0));
}

// ------------------------------------------------------------------ poll

const FAST_POLL_INTERVAL_MS = 5000;
const MEDIUM_POLL_INTERVAL_MS = 15_000;
const SLOW_POLL_INTERVAL_MS = 60_000;
const FAST_POLL_MAX_RANGE_MINUTES = 60;
const MEDIUM_POLL_MAX_RANGE_MINUTES = DAY_MINUTES;

/**
 * "Every few seconds" (design 10.1) for an hour or less. Up to a day the answer
 * holds up to 1440 rows and changes once a minute, so it is asked for every
 * 15 s; longer ranges are read in steps of 5 or 15 minutes whose newest step
 * grows once a minute, so once a minute is enough.
 */
export function metricsPollIntervalMs(rangeMinutes: number): number {
  let intervalMs = SLOW_POLL_INTERVAL_MS;
  if (rangeMinutes <= FAST_POLL_MAX_RANGE_MINUTES) {
    intervalMs = FAST_POLL_INTERVAL_MS;
  } else if (rangeMinutes <= MEDIUM_POLL_MAX_RANGE_MINUTES) {
    intervalMs = MEDIUM_POLL_INTERVAL_MS;
  }
  return Math.max(intervalMs, MIN_POLL_INTERVAL_MS);
}
