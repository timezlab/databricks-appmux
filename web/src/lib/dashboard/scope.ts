/**
 * What the dashboard asks the metrics API for: the time ranges a person can
 * choose, the "scope" (the platform, every app, or one app), the window sent as
 * `from`/`to`, and how often to ask. No React and no fetch here, so the rules
 * test without a browser.
 *
 * The contract (admin-api.md, `GET /metrics` and `MetricsResponse`): `app` is a
 * slug or `_platform`; without it a platform admin gets every app and the
 * platform row; a window over 1440 minutes is a `400`.
 */
import type { AppSummary } from "@/lib/api/schemas";
import { isValidSlug } from "@/lib/slug-link";
import { MIN_POLL_INTERVAL_MS } from "@/lib/poll/schedule";

/** `app_slug` of the platform-wide row (data model 4.8). Not a valid app slug: the pattern has no underscore. */
export const PLATFORM_APP_SLUG = "_platform";

/** Longest window the API accepts; a longer one is a `400` (admin-api.md, MetricsResponse). */
export const MAX_WINDOW_MINUTES = 1440;

const MINUTE_MS = 60_000;

// ---------------------------------------------------------------- ranges

export interface TimeRange {
  id: string;
  label: string;
  minutes: number;
}

/**
 * The choices, shortest first. They stop at one day because that is the longest
 * window one request may cover; the data is kept for 7 days (data model 8), but
 * reading older days would need a window picker, which v1 does not have.
 */
export const TIME_RANGES = [
  { id: "15m", label: "Last 15 minutes", minutes: 15 },
  { id: "1h", label: "Last hour", minutes: 60 },
  { id: "6h", label: "Last 6 hours", minutes: 360 },
  { id: "24h", label: "Last 24 hours", minutes: MAX_WINDOW_MINUTES },
] as const satisfies readonly TimeRange[];

export type TimeRangeId = (typeof TIME_RANGES)[number]["id"];

export const DEFAULT_RANGE_ID: TimeRangeId = "1h";

export function isTimeRangeId(value: unknown): value is TimeRangeId {
  return TIME_RANGES.some((range) => range.id === value);
}

/** The range with this id, or `null` (a select's value is a string, not yet a known id). */
export function timeRangeById(id: string): TimeRange | null {
  return TIME_RANGES.find((range) => range.id === id) ?? null;
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
 * design 10.2), and a window that starts and ends on a minute boundary keeps
 * the chart still for a whole minute instead of sliding on every poll. Both
 * ends come from the browser clock, so the length is exactly `minutes` even
 * when that clock is off; only the position shifts.
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

// ----------------------------------------------------------------- scope

export type DashboardScope =
  { kind: "platform" } | { kind: "apps" } | { kind: "app"; slug: string };

const APP_KEY_PREFIX = "app:";

/** A string for a `<select>` value and for `usePoll`'s `resetKey`. */
export function scopeKey(scope: DashboardScope): string {
  switch (scope.kind) {
    case "platform":
      return "platform";
    case "apps":
      return "apps";
    case "app":
      return `${APP_KEY_PREFIX}${scope.slug}`;
  }
}

/** The scope a key names, or `null`; a slug is checked, so only a real one reaches a query. */
export function parseScopeKey(key: string): DashboardScope | null {
  if (key === "platform") return { kind: "platform" };
  if (key === "apps") return { kind: "apps" };
  if (key.startsWith(APP_KEY_PREFIX)) {
    const slug = key.slice(APP_KEY_PREFIX.length);
    return isValidSlug(slug) ? { kind: "app", slug } : null;
  }
  return null;
}

/** The query of `getMetrics` for a scope and a window. */
export function metricsQueryFor(
  scope: DashboardScope,
  window: MetricsWindow,
): { app?: string; from: string; to: string } {
  switch (scope.kind) {
    case "platform":
      return { app: PLATFORM_APP_SLUG, ...window };
    case "apps":
      return { ...window };
    case "app":
      return { app: scope.slug, ...window };
  }
}

export interface ScopeOption {
  key: string;
  label: string;
}

/**
 * What the scope select offers. A platform admin may read the platform row, the
 * whole table and any app (`CAN_MANAGE` on the platform); anyone else may read
 * only the apps where they hold `CAN_MANAGE`, because the API refuses the rest.
 * `display_name` is text from the API: it is a label, never markup.
 */
export function scopeOptions(input: {
  isPlatformAdmin: boolean;
  apps: readonly AppSummary[];
}): ScopeOption[] {
  const apps = input.apps
    .filter(
      (app) =>
        isValidSlug(app.slug) &&
        (input.isPlatformAdmin || app.permission === "CAN_MANAGE"),
    )
    .toSorted((a, b) => (a.slug < b.slug ? -1 : a.slug > b.slug ? 1 : 0))
    .map((app) => ({
      key: scopeKey({ kind: "app", slug: app.slug }),
      label: `${app.display_name} (${app.slug})`,
    }));
  if (!input.isPlatformAdmin) return apps;
  return [
    { key: "platform", label: "Platform: all traffic and memory" },
    { key: "apps", label: "All apps: comparison table" },
    ...apps,
  ];
}

/** A platform admin starts on the platform; an app admin on their first app; nobody, on nothing. */
export function defaultScopeKey(
  options: readonly ScopeOption[],
): string | null {
  return options[0]?.key ?? null;
}

/** The chosen key while it is still offered (an app can be deleted or a role lost between two polls), else the default. */
export function resolveScopeKey(
  selected: string | null,
  options: readonly ScopeOption[],
): string | null {
  if (selected !== null && options.some((option) => option.key === selected)) {
    return selected;
  }
  return defaultScopeKey(options);
}

// ------------------------------------------------------------------ poll

const FAST_POLL_INTERVAL_MS = 5000;
const SLOW_POLL_INTERVAL_MS = 15_000;
const FAST_POLL_MAX_RANGE_MINUTES = 60;

/**
 * "Every few seconds" (design 10.1) for an answer of an hour or less for one
 * series (at most 60 rows). A day (up to 1440 rows) or every app's rows (up to
 * the 10000-row cap) is large and the data changes only once a minute, so those
 * are asked for every 15 s. That is slower than the "every few seconds" of
 * FR-051 and design 10.1 for those two cases, and neither text says so yet.
 */
export function metricsPollIntervalMs(
  scope: DashboardScope,
  rangeMinutes: number,
): number {
  const isSmall =
    scope.kind !== "apps" && rangeMinutes <= FAST_POLL_MAX_RANGE_MINUTES;
  return Math.max(
    isSmall ? FAST_POLL_INTERVAL_MS : SLOW_POLL_INTERVAL_MS,
    MIN_POLL_INTERVAL_MS,
  );
}
