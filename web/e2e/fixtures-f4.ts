/**
 * The dashboard's worlds (lane F4): the hour just ended with a breakdown by app
 * that lies in the same hour, and the last 7 days at 15 minutes.
 *
 * The mock's default `GET /metrics` draws the contract's rows minute by minute
 * for the window asked, up to a day, as the API did before `step`; its
 * `GET /metrics/breakdown` is the contract's example, a 40-minute window in
 * October 2026. A scenario sees the route and not the query (server.ts passes
 * none to `scenarioAnswer`), so each world answers the one window the dashboard
 * asks for at its range, taken from the clock when the call comes, as the page's
 * `metricsWindow` does (the minutes before the current one):
 *
 * - `breakdown-live`: the breakdown of the last hour at `1m`; `GET /metrics`
 *   stays the default, so the hour before (the change figures) still answers.
 * - `metrics-week`: the `_platform` rows and the breakdown of the last 7 days at
 *   `15m`. Only the platform row: the world is the dashboard's, not an app's
 *   Insights.
 *
 * The values follow a day in UTC and are set by the time of each step, not by the
 * call, so two polls draw the same chart. One afternoon a week an app is down
 * (its requests answer 5xx and it is not running), and an app's egress is dropped
 * for an hour about once a day, so the error split, the apps running and the
 * drops have something to show. Bodies are shaped like the contract's examples
 * and parse with the UI's own schemas.
 */
import type { ScenarioAnswer } from "./fixtures.ts";

export const F4_SCENARIOS = ["breakdown-live", "metrics-week"] as const;
export type F4Scenario = (typeof F4_SCENARIOS)[number];

export function isF4Scenario(name: string): name is F4Scenario {
  return (F4_SCENARIOS as readonly string[]).includes(name);
}

type ReadExample = (name: string) => unknown;
type JsonObject = Record<string, unknown>;

const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;

interface WindowShape {
  minutes: number;
  stepMinutes: number;
  step: "1m" | "15m";
}

const LAST_HOUR: WindowShape = { minutes: 60, stepMinutes: 1, step: "1m" };
const LAST_WEEK: WindowShape = {
  minutes: 7 * 24 * 60,
  stepMinutes: 15,
  step: "15m",
};

/** How many apps the page asks the breakdown for (`limit`, the contract's default). */
const BREAKDOWN_LIMIT = 8;

type AppSeed = readonly [
  slug: string,
  displayName: string,
  shareOfRequests: number,
  rssMb: number,
];

/**
 * Eleven apps, so the breakdown names eight and adds up the other three. The
 * first three are the AppList example's; the shares add up to 1.
 */
const APPS: readonly AppSeed[] = [
  ["docs", "Team docs", 0.3, 410],
  ["sales", "Sales dashboard", 0.22, 340],
  ["reports", "Weekly reports", 0.12, 520],
  ["hr-onboarding", "HR onboarding", 0.09, 210],
  ["data-quality", "Data quality monitor", 0.07, 640],
  ["churn-explorer", "Churn explorer", 0.06, 380],
  ["wiki", "Team wiki", 0.05, 190],
  ["okr-tracker", "OKR tracker", 0.04, 150],
  ["campaign-roi", "Campaign ROI", 0.025, 230],
  ["inventory", "Inventory levels", 0.015, 170],
  ["finance-close", "Finance close checklist", 0.01, 260],
];

/** The app that is down one afternoon a week. */
const DOWN_APP_SLUG = "reports";
/** The app whose calls out are dropped about once a day, and the global rule that drops them. */
const DROPPED_APP_SLUG = "data-quality";
const DROPPING_RULE_ID = "global:1";

const SHARE_OF_4XX = 0.022;
const SHARE_OF_5XX = 0.004;
const SHARE_OF_5XX_WHEN_DOWN = 0.6;
const SHARE_OF_3XX = 0.027;
const BYTES_PER_RESPONSE = 12_288;
const EDGE_RSS_MB = 160;

function iso(ms: number): string {
  return new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z");
}

function isJsonObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function asObject(value: unknown, what: string): JsonObject {
  if (!isJsonObject(value)) throw new Error(`${what} is not an object`);
  return value;
}

/** The `_platform` row of the MetricsResponse example: the shape of a row, and its requests per minute. */
function platformTemplate(readExample: ReadExample): JsonObject {
  const example = asObject(readExample("MetricsResponse"), "MetricsResponse");
  const rows = Array.isArray(example.items) ? example.items : [];
  const row: unknown = rows.find(
    (item) => asObject(item, "a MetricsResponse row").app_slug === "_platform",
  );
  return asObject(row, "the _platform row of MetricsResponse");
}

function numbersOf(value: unknown): number[] {
  return Array.isArray(value) ? value.map(Number) : [];
}

/**
 * About 0.1 to 1.2 of an average minute: low at night in UTC, highest in the
 * middle of the afternoon, with a ripple per app and per minute so the stacks
 * do not move as one and an hour is not flat.
 */
function trafficFactor(stepStartMs: number, appIndex: number): number {
  const hours = stepStartMs / HOUR_MS;
  const minute = stepStartMs / MINUTE_MS;
  const daily = 0.5 + 0.5 * Math.sin((2 * Math.PI * (hours - 9)) / 24);
  const ripple = 0.08 * Math.sin(hours * 1.3 + appIndex * 2.1);
  const jitter =
    0.12 * Math.sin(minute * 0.9 + appIndex) * Math.sin(minute * 0.23);
  return 0.2 + 0.8 * daily + ripple + jitter;
}

/** 14:00 to 17:00 UTC on one day of the week. */
function isDownAfternoon(stepStartMs: number): boolean {
  const day = Math.floor(stepStartMs / DAY_MS);
  const hour = (stepStartMs - day * DAY_MS) / HOUR_MS;
  return day % 7 === 3 && hour >= 14 && hour < 17;
}

/** One hour out of 29. */
function isDroppingHour(stepStartMs: number): boolean {
  return Math.floor(stepStartMs / HOUR_MS) % 29 === 0;
}

interface Step {
  startMs: number;
  /** The minutes of the step inside the window: fewer than the step for a step the window cuts. */
  minutes: number;
}

/** The window the page asks for at that range, now, and its steps on UTC boundaries. */
function stepsOf(shape: WindowShape): {
  fromMs: number;
  toMs: number;
  steps: Step[];
} {
  const toMs = Math.floor(Date.now() / MINUTE_MS) * MINUTE_MS;
  const fromMs = toMs - shape.minutes * MINUTE_MS;
  const stepMs = shape.stepMinutes * MINUTE_MS;
  const steps: Step[] = [];
  for (
    let startMs = Math.floor(fromMs / stepMs) * stepMs;
    startMs < toMs;
    startMs += stepMs
  ) {
    const inside = Math.min(startMs + stepMs, toMs) - Math.max(startMs, fromMs);
    steps.push({ startMs, minutes: inside / MINUTE_MS });
  }
  return { fromMs, toMs, steps };
}

/** A breakdown point (`{minute, requests, r4xx, r5xx, rss_mb_max, bytes_out, egress_dropped, lat_hist}`). */
function appPoint(
  seed: AppSeed,
  appIndex: number,
  step: Step,
  perMinute: number,
  histogram: readonly number[],
): JsonObject {
  const [slug, , share, rssMb] = seed;
  const factor = trafficFactor(step.startMs, appIndex);
  const requests = Math.round(perMinute * step.minutes * share * factor);
  const isDown = slug === DOWN_APP_SLUG && isDownAfternoon(step.startMs);
  const isDropping = slug === DROPPED_APP_SLUG && isDroppingHour(step.startMs);
  const histogramTotal = histogram.reduce((sum, count) => sum + count, 0);
  return {
    minute: iso(step.startMs),
    requests,
    r4xx: Math.round(requests * SHARE_OF_4XX),
    r5xx: Math.round(
      requests * (isDown ? SHARE_OF_5XX_WHEN_DOWN : SHARE_OF_5XX),
    ),
    rss_mb_max: isDown ? 0 : Math.round(rssMb * (0.85 + 0.2 * factor)),
    bytes_out: requests * BYTES_PER_RESPONSE,
    egress_dropped: isDropping ? Math.max(1, Math.round(step.minutes / 5)) : 0,
    lat_hist: histogram.map((count) =>
      histogramTotal === 0
        ? 0
        : Math.round((count * requests) / histogramTotal),
    ),
  };
}

/** Every app's point of one step, in the order of APPS. */
function pointsOf(
  step: Step,
  perMinute: number,
  histogram: readonly number[],
): JsonObject[] {
  return APPS.map((seed, appIndex) =>
    appPoint(seed, appIndex, step, perMinute, histogram),
  );
}

function sumOf(points: readonly JsonObject[], field: string): number {
  return points.reduce((sum, point) => sum + Number(point[field]), 0);
}

function sumHistograms(points: readonly JsonObject[]): number[] {
  const sums: number[] = [];
  for (const point of points) {
    numbersOf(point.lat_hist).forEach((count, index) => {
      sums[index] = (sums[index] ?? 0) + count;
    });
  }
  return sums;
}

/** The points of several apps added up step by step, every column summed (the contract's `other`). */
function summedPoint(
  minute: string,
  points: readonly JsonObject[],
): JsonObject {
  return {
    minute,
    requests: sumOf(points, "requests"),
    r4xx: sumOf(points, "r4xx"),
    r5xx: sumOf(points, "r5xx"),
    rss_mb_max: sumOf(points, "rss_mb_max"),
    bytes_out: sumOf(points, "bytes_out"),
    egress_dropped: sumOf(points, "egress_dropped"),
    lat_hist: sumHistograms(points),
  };
}

/** The `_platform` row of a step: the apps added up, plus the slot's own figures. */
function platformRow(
  template: JsonObject,
  step: Step,
  points: readonly JsonObject[],
): JsonObject {
  const requests = sumOf(points, "requests");
  const r4xx = sumOf(points, "r4xx");
  const r5xx = sumOf(points, "r5xx");
  const r3xx = Math.round(requests * SHARE_OF_3XX);
  const dropped = sumOf(points, "egress_dropped");
  const perMinute = Number(template.requests);
  const load = requests / Math.max(1, perMinute * step.minutes);
  const scaled = (field: string) =>
    Math.round(Number(template[field]) * step.minutes * load);
  const isAppDown = isDownAfternoon(step.startMs);
  return {
    ...template,
    app_slug: "_platform",
    minute: iso(step.startMs),
    requests,
    r2xx: requests - r3xx - r4xx - r5xx,
    r3xx,
    r4xx,
    r5xx,
    upstream_errors: Math.round(r5xx * 0.6),
    limited: 0,
    lat_hist: sumHistograms(points),
    bytes_in: scaled("bytes_in"),
    bytes_out: sumOf(points, "bytes_out"),
    in_flight_max: Math.max(1, Math.round(requests / step.minutes / 60)),
    ws_opened: 0,
    ws_open: 0,
    rss_mb_max: 0,
    restarts: isAppDown ? 1 : 0,
    egress_bytes_out: scaled("egress_bytes_out"),
    egress_bytes_in: scaled("egress_bytes_in"),
    egress_dropped: dropped,
    egress_dropped_by_rule: dropped > 0 ? { [DROPPING_RULE_ID]: dropped } : {},
    mem_used_mb: sumOf(points, "rss_mb_max") + EDGE_RSS_MB,
    children_running: isAppDown ? APPS.length - 1 : APPS.length,
    children_registered: APPS.length,
    admission_rejections: 0,
    registry_errors: 0,
    edge_rss_mb: EDGE_RSS_MB,
  };
}

function metricsBody(shape: WindowShape, readExample: ReadExample): JsonObject {
  const template = platformTemplate(readExample);
  const histogram = numbersOf(template.lat_hist);
  const perMinute = Number(template.requests);
  const { fromMs, toMs, steps } = stepsOf(shape);
  return {
    from: iso(fromMs),
    to: iso(toMs),
    truncated: false,
    items: steps.map((step) =>
      platformRow(template, step, pointsOf(step, perMinute, histogram)),
    ),
  };
}

function breakdownBody(
  shape: WindowShape,
  readExample: ReadExample,
): JsonObject {
  const template = platformTemplate(readExample);
  const histogram = numbersOf(template.lat_hist);
  const perMinute = Number(template.requests);
  const { fromMs, toMs, steps } = stepsOf(shape);
  const byStep = steps.map((step) => pointsOf(step, perMinute, histogram));
  const totalOf = (appIndex: number) =>
    byStep.reduce((sum, points) => sum + Number(points[appIndex]?.requests), 0);
  // Most requests first, then by slug: the contract's order.
  const ranked = APPS.map((seed, appIndex) => ({
    slug: seed[0],
    displayName: seed[1],
    appIndex,
    total: totalOf(appIndex),
  })).toSorted((a, b) =>
    a.total === b.total ? (a.slug < b.slug ? -1 : 1) : b.total - a.total,
  );
  const named = ranked.slice(0, BREAKDOWN_LIMIT);
  const rest = ranked.slice(BREAKDOWN_LIMIT);
  return {
    from: iso(fromMs),
    to: iso(toMs),
    step: shape.step,
    series: named.map((app) => ({
      app_slug: app.slug,
      display_name: app.displayName,
      points: byStep.map((points) => points[app.appIndex]),
    })),
    other:
      rest.length === 0
        ? null
        : byStep.map((points, index) =>
            summedPoint(
              iso(steps[index]?.startMs ?? fromMs),
              rest.map((app) => points[app.appIndex] ?? {}),
            ),
          ),
  };
}

function ok(body: JsonObject): ScenarioAnswer {
  return { status: 200, body };
}

/** The answer a dashboard world gives to a route (`GET /metrics`), or `null` to leave it to the examples. */
export function f4ScenarioAnswer(
  scenario: F4Scenario,
  route: string,
  readExample: ReadExample,
): ScenarioAnswer | null {
  switch (scenario) {
    case "breakdown-live":
      return route === "GET /metrics/breakdown"
        ? ok(breakdownBody(LAST_HOUR, readExample))
        : null;
    case "metrics-week":
      if (route === "GET /metrics")
        return ok(metricsBody(LAST_WEEK, readExample));
      if (route === "GET /metrics/breakdown") {
        return ok(breakdownBody(LAST_WEEK, readExample));
      }
      return null;
  }
}
