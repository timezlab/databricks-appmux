/**
 * Everything the dashboard shows, as plain text and numbers: the answer of
 * `GET /metrics` and the chosen scope go in; stat tiles, chart specs (with the
 * tooltip texts and the table twin of each), notices and fact lists come out.
 * The components copy these into elements and add no rule of their own, so the
 * wording and the arithmetic are tested here without a browser.
 *
 * Every string that comes from the API (a slug, a rule id) is passed on as text;
 * nothing here builds markup or a link.
 */
import type { MetricsResponse } from "@/lib/api/schemas";
import {
  CAPACITY_DANGER_RATIO,
  CAPACITY_WARNING_RATIO,
  capacityLevel,
} from "@/lib/capacity";
import type { CapacityLevel } from "@/lib/capacity";
import { clockLabel } from "./chart";
import { PLATFORM_APP_SLUG } from "./scope";
import type { DashboardScope } from "./scope";
import {
  LATENCY_BUCKET_BOUNDS_MS,
  appComparison,
  bucketRows,
  describeLatency,
  dropsByRule,
  latencyBucketLabel,
  percentileBucket,
  rowsInDomain,
  summarizeRows,
  windowDomain,
} from "./series";
import type {
  AppComparisonRow,
  MetricBucket,
  RuleDrops,
  SeriesSummary,
} from "./series";
import {
  formatAbsoluteTime,
  formatBytes,
  formatCount,
  formatDurationMs,
  formatMegabytes,
  formatPercent,
  pluralize,
} from "@/lib/format";

// ------------------------------------------------------------------ types

/** Which series colour a mark takes; `series-style.ts` maps each tone to a `--chart-N` token. */
export type ChartTone = "primary" | "warning" | "danger";

/**
 * The pattern laid over a mark (and its legend swatch) besides its colour. The
 * three tones differ little in lightness, so a person who cannot tell the hues
 * apart still tells a plain segment from a striped one from a dotted one.
 */
export type ToneTexture = "none" | "stripes" | "dots";

export const TONE_TEXTURE: Record<ChartTone, ToneTexture> = {
  primary: "none",
  warning: "stripes",
  danger: "dots",
};

export interface LegendItem {
  label: string;
  tone: ChartTone;
}

/** The accessible twin of a chart: the first cell of each row names the row. */
export interface ChartTable {
  headers: string[];
  rows: { key: string; cells: string[] }[];
}

interface ChartBase {
  id: "requests" | "latency" | "memory" | "egress";
  /** "Requests per minute" or "Requests per 5 minutes". */
  title: string;
  /** What a screen reader hears for the picture. */
  description: string;
  /** Two or more series need a legend; one is named by the title. */
  legend: LegendItem[];
  /** The time (or bucket) label of each column, parallel to the data. */
  labels: string[];
  /** The hover text of each column, parallel to the data. A value is never only in a tooltip: the table has it. */
  tips: string[];
  /** The most labels under the axis. */
  maxTicks: number;
  /** Text under the axis. */
  xCaption: string;
  table: ChartTable;
  /** When set, the chart has nothing to draw and the message replaces it. */
  emptyMessage: string | null;
}

export interface ColumnChartSpec extends ChartBase {
  kind: "columns";
  /** The tone of each stacked segment, bottom first. */
  tones: ChartTone[];
  /** Per column the segment values, or `null` for no data. */
  columns: (number[] | null)[];
  /** Labels over chosen columns ("p50"). */
  highlights: { index: number; label: string }[];
}

export interface LineChartSpec extends ChartBase {
  kind: "line";
  /** The tone of the line itself. */
  tone: ChartTone;
  values: (number | null)[];
  reference: number | null;
  referenceLabel: string | null;
  /** The tone of the level line (the memory limit). */
  referenceTone: ChartTone;
}

export type ChartSpec = ColumnChartSpec | LineChartSpec;

export type MeterTone = "success" | "warning" | "danger";

export interface StatTile {
  key: "requests" | "errors" | "p50" | "p95" | "memory" | "egress";
  label: string;
  value: string;
  detail: string | null;
  meter: { label: string; value: number; max: number; tone: MeterTone } | null;
}

export interface FactRow {
  key: string;
  label: string;
  value: string;
}

export interface ModelNotice {
  key: "truncated" | "gap";
  tone: "warning" | "info";
  title: string;
  detail: string;
}

interface ModelBase {
  /** "2026-10-30 08:00 UTC to 2026-10-30 09:00 UTC": the window the answer covers. */
  windowText: string;
  notices: ModelNotice[];
}

export interface EmptyModel extends ModelBase {
  kind: "empty";
  title: string;
  message: string;
}

export interface SeriesModel extends ModelBase {
  kind: "series";
  tiles: StatTile[];
  charts: ChartSpec[];
  drops: RuleDrops[];
  facts: FactRow[];
}

export interface AppsModel extends ModelBase {
  kind: "apps";
  rows: AppComparisonRow[];
}

export type DashboardModel = EmptyModel | SeriesModel | AppsModel;

// ----------------------------------------------------------------- tuning

const REQUEST_LABELS_MAX_TICKS = 6;
/** The latency chart has 11 columns of about 50 px; every one gets its label. */
const LATENCY_MAX_TICKS = 11;
const RATE_DECIMALS_BELOW = 10;
const SHARE_DECIMALS_BELOW_PERCENT = 10;
const SHARE_FLOOR_PERCENT = 0.1;
const PERCENT = 100;
const MEDIAN_PERCENT = 50;
const TAIL_PERCENT = 95;

/**
 * The memory thresholds are the capacity strip's (`lib/capacity.ts`): one pair
 * of ratios for every place that shows memory against its limit.
 */
export const MEMORY_WARNING_RATIO = CAPACITY_WARNING_RATIO;
export const MEMORY_DANGER_RATIO = CAPACITY_DANGER_RATIO;

const TONE_BY_LEVEL = {
  normal: "success",
  warning: "warning",
  danger: "danger",
} as const satisfies Record<CapacityLevel, MeterTone>;

export function memoryTone(usedMb: number, limitMb: number): MeterTone {
  return TONE_BY_LEVEL[capacityLevel(usedMb, limitMb)];
}

// --------------------------------------------------------------- wording

const NO_DATA_TEXT = "no data";
const NO_REQUESTS_TEXT = "no requests";

/** "0.7%", "25%", "less than 0.1%": a small share keeps its decimal, a big one is whole. */
function shareText(part: number, whole: number): string {
  if (!(whole > 0)) return "0%";
  const percent = (part / whole) * PERCENT;
  if (percent <= 0) return "0%";
  if (percent >= SHARE_DECIMALS_BELOW_PERCENT) {
    return `${Math.round(percent)}%`;
  }
  if (percent < SHARE_FLOOR_PERCENT) return `less than ${SHARE_FLOOR_PERCENT}%`;
  return `${Number(percent.toFixed(1))}%`;
}

function rateText(perMinute: number): string {
  return perMinute < RATE_DECIMALS_BELOW
    ? String(Number(perMinute.toFixed(1)))
    : formatCount(perMinute);
}

function perText(bucketMinutes: number): string {
  return bucketMinutes === 1 ? "minute" : `${bucketMinutes} minutes`;
}

/** The range of a latency bucket: "Up to 5 ms", "Over 500 ms up to 1 s", "Over 5 s". */
function latencyRangeText(index: number): string {
  const upper = LATENCY_BUCKET_BOUNDS_MS[index];
  const lower = LATENCY_BUCKET_BOUNDS_MS[index - 1];
  if (upper === undefined) {
    return lower === undefined ? "Any time" : `Over ${formatDurationMs(lower)}`;
  }
  return lower === undefined
    ? `Up to ${formatDurationMs(upper)}`
    : `Over ${formatDurationMs(lower)} up to ${formatDurationMs(upper)}`;
}

// ----------------------------------------------------------------- build

function scopeSlug(scope: DashboardScope): string | null {
  switch (scope.kind) {
    case "platform":
      return PLATFORM_APP_SLUG;
    case "app":
      return scope.slug;
    case "apps":
      return null;
  }
}

function emptyModel(
  base: ModelBase,
  scope: DashboardScope,
  isWindowUsable: boolean,
): EmptyModel {
  let message: string;
  if (!isWindowUsable) {
    message = "The answer carries no usable time window.";
  } else if (scope.kind === "platform") {
    message = "The platform wrote no metrics in this window.";
  } else if (scope.kind === "apps") {
    message = "No app wrote metrics in this window.";
  } else {
    message =
      "The app wrote no metrics in this window. It may have been stopped for all of it.";
  }
  return {
    ...base,
    kind: "empty",
    title: "No metrics in this window",
    message,
  };
}

function truncatedNotice(response: MetricsResponse): ModelNotice {
  return {
    key: "truncated",
    tone: "warning",
    title: "The answer was cut to its newest rows",
    detail:
      `The server limits how many rows one answer holds, so older minutes were left out and the window starts at ` +
      `${formatAbsoluteTime(response.from)}, later than the range you chose. Pick one app or a shorter range to see all of it.`,
  };
}

function gapNotice(minutes: number, scope: DashboardScope): ModelNotice {
  const reason =
    scope.kind === "platform"
      ? "The edge wrote no platform metrics for those minutes (for example while the database was down)."
      : "The app was stopped, or no metrics were written for those minutes (for example while the database was down).";
  return {
    key: "gap",
    tone: "info",
    title: `${pluralize(minutes, "minute")} without data`,
    detail: `${reason} They are left blank in the charts, not drawn as zero.`,
  };
}

export function buildDashboardModel(
  response: MetricsResponse,
  scope: DashboardScope,
): DashboardModel {
  const base: ModelBase = {
    windowText: `${formatAbsoluteTime(response.from)} to ${formatAbsoluteTime(response.to)}`,
    notices: response.truncated ? [truncatedNotice(response)] : [],
  };
  const domain = windowDomain(response.from, response.to);
  if (domain === null) return emptyModel(base, scope, false);

  const slug = scopeSlug(scope);
  const inWindow = rowsInDomain(response.items, domain);

  if (slug === null) {
    const table = appComparison(inWindow);
    return table.length === 0
      ? emptyModel(base, scope, true)
      : { ...base, kind: "apps", rows: table };
  }

  const rows = inWindow.filter((row) => row.app_slug === slug);
  if (rows.length === 0) return emptyModel(base, scope, true);

  const summary = summarizeRows(rows);
  const notices =
    summary.gapMinutes > 0
      ? [...base.notices, gapNotice(summary.gapMinutes, scope)]
      : base.notices;
  const { bucketMinutes, buckets } = bucketRows(rows, domain);
  const context: ChartContext = {
    domainStartMs: domain.startMs,
    bucketMinutes,
    buckets,
    summary,
    windowText: base.windowText,
    isPlatform: scope.kind === "platform",
  };

  return {
    ...base,
    notices,
    kind: "series",
    tiles: buildTiles(summary, scope),
    charts: [
      requestsChart(context),
      latencyChart(context),
      memoryChart(context),
      egressChart(context),
    ],
    drops: dropsByRule(rows),
    facts: buildFacts(summary, scope),
  };
}

// ----------------------------------------------------------------- tiles

/** Memory figures of the platform rows are the edge's own; an app's are its resident memory. */
function usesPlatformMemory(summary: SeriesSummary, isPlatform: boolean) {
  return isPlatform && summary.memUsedMbMax !== null;
}

function buildTiles(summary: SeriesSummary, scope: DashboardScope): StatTile[] {
  const p50 = percentileBucket(summary.latHist, MEDIAN_PERCENT);
  const p95 = percentileBucket(summary.latHist, TAIL_PERCENT);
  const hasRequests = summary.requests > 0;

  const requestsDetail =
    summary.reportedMinutes > 0
      ? `${rateText(summary.requests / summary.reportedMinutes)} per minute over ${pluralize(summary.reportedMinutes, "minute")} with data`
      : null;

  return [
    {
      key: "requests",
      label: "Requests",
      value: formatCount(summary.requests),
      detail: requestsDetail,
      meter: null,
    },
    {
      key: "errors",
      label: "Server errors",
      value: formatCount(summary.r5xx),
      detail: hasRequests
        ? `${shareText(summary.r5xx, summary.requests)} of requests`
        : NO_REQUESTS_TEXT,
      meter: null,
    },
    {
      key: "p50",
      label: "Latency p50",
      value: describeLatency(p50),
      detail: p50 === null ? null : "half of the requests were this fast",
      meter: null,
    },
    {
      key: "p95",
      label: "Latency p95",
      value: describeLatency(p95),
      detail: p95 === null ? null : "95 of 100 requests were this fast",
      meter: null,
    },
    memoryTile(summary, scope.kind === "platform"),
    {
      key: "egress",
      label: "Egress drops",
      value: formatCount(summary.egressDropped),
      detail:
        summary.egressDropped > 0
          ? "packets dropped by egress rules"
          : "nothing was blocked",
      meter: null,
    },
  ];
}

function memoryTile(summary: SeriesSummary, isPlatform: boolean): StatTile {
  const used = summary.memUsedMbMax;
  if (!isPlatform || used === null) {
    return {
      key: "memory",
      label: "Memory peak",
      value: formatMegabytes(summary.rssMbMax),
      detail: isPlatform
        ? "highest resident memory in the window"
        : "highest resident memory of the app",
      meter: null,
    };
  }
  const limit = summary.memLimitMb;
  if (limit === null || limit <= 0) {
    return {
      key: "memory",
      label: "Memory peak",
      value: formatMegabytes(used),
      detail: "highest memory used by the platform",
      meter: null,
    };
  }
  return {
    key: "memory",
    label: "Memory peak",
    value: formatMegabytes(used),
    detail: `${formatPercent(used, limit)} of the ${formatMegabytes(limit)} limit`,
    meter: {
      label: "Platform memory peak",
      value: used,
      max: limit,
      tone: memoryTone(used, limit),
    },
  };
}

// ----------------------------------------------------------------- facts

function buildFacts(summary: SeriesSummary, scope: DashboardScope): FactRow[] {
  const facts: FactRow[] = [
    {
      key: "restarts",
      label: "Restarts",
      value: formatCount(summary.restarts),
    },
    {
      key: "upstream-errors",
      label: "Answered 502 or 504 by the edge",
      value: formatCount(summary.upstreamErrors),
    },
    {
      key: "limited",
      label: "Refused by limits (429, 413)",
      value: formatCount(summary.limited),
    },
    {
      key: "in-flight",
      label: "Most requests in flight at once",
      value: formatCount(summary.inFlightMax),
    },
    {
      key: "bytes-in",
      label: "Data received",
      value: formatBytes(summary.bytesIn),
    },
    {
      key: "bytes-out",
      label: "Data sent",
      value: formatBytes(summary.bytesOut),
    },
    {
      key: "websockets",
      label: "WebSockets opened",
      value: formatCount(summary.wsOpened),
    },
    {
      key: "egress-out",
      label: "Sent through the egress proxy",
      value: formatBytes(summary.egressBytesOut),
    },
    {
      key: "egress-in",
      label: "Received through the egress proxy",
      value: formatBytes(summary.egressBytesIn),
    },
  ];
  if (scope.kind !== "platform") return facts;

  if (summary.childrenRunning !== null && summary.childrenRegistered !== null) {
    facts.push({
      key: "children",
      label: "Apps",
      value: `${formatCount(summary.childrenRunning)} of ${formatCount(summary.childrenRegistered)} running`,
    });
  }
  if (summary.admissionRejections !== null) {
    facts.push({
      key: "admission",
      label: "Starts refused for lack of memory",
      value: formatCount(summary.admissionRejections),
    });
  }
  if (summary.registryErrors !== null) {
    facts.push({
      key: "registry-errors",
      label: "Registry errors",
      value: formatCount(summary.registryErrors),
    });
  }
  if (summary.registryMsMax !== null) {
    facts.push({
      key: "registry-slowest",
      label: "Slowest registry call",
      value: formatDurationMs(summary.registryMsMax),
    });
  }
  if (summary.edgeRssMbMax !== null) {
    facts.push({
      key: "edge-memory",
      label: "Edge memory peak",
      value: formatMegabytes(summary.edgeRssMbMax),
    });
  }
  return facts;
}

// ---------------------------------------------------------------- charts

interface ChartContext {
  domainStartMs: number;
  bucketMinutes: number;
  buckets: MetricBucket[];
  summary: SeriesSummary;
  windowText: string;
  isPlatform: boolean;
}

function spansTwoDays(buckets: readonly MetricBucket[]): boolean {
  const first = buckets[0];
  const last = buckets[buckets.length - 1];
  if (first === undefined || last === undefined) return false;
  return (
    new Date(first.startMs).getUTCDate() !== new Date(last.startMs).getUTCDate()
  );
}

function timeLabels(context: ChartContext): string[] {
  const withDate = spansTwoDays(context.buckets);
  return context.buckets.map((bucket) => clockLabel(bucket.startMs, withDate));
}

/** The start of a bucket as written in a tooltip or a table; the first bucket may start before the window, and says so. */
function bucketTime(bucket: MetricBucket, context: ChartContext): string {
  const start = formatAbsoluteTime(
    Math.max(bucket.startMs, context.domainStartMs),
  );
  if (bucket.isPartial) return `${start} (partial)`;
  return context.bucketMinutes === 1
    ? start
    : `${start}, ${context.bucketMinutes} minutes`;
}

/** The first table cell does not repeat the length of the bucket: the title says it. */
function rowTime(bucket: MetricBucket, context: ChartContext): string {
  const start = formatAbsoluteTime(
    Math.max(bucket.startMs, context.domainStartMs),
  );
  return bucket.isPartial ? `${start} (partial)` : start;
}

function noDataCells(count: number): string[] {
  return new Array<string>(count).fill(NO_DATA_TEXT);
}

function requestsChart(context: ChartContext): ColumnChartSpec {
  const { buckets, summary } = context;
  const columns = buckets.map((bucket) =>
    bucket.hasData
      ? [
          Math.max(bucket.requests - bucket.r4xx - bucket.r5xx, 0),
          bucket.r4xx,
          bucket.r5xx,
        ]
      : null,
  );
  const legend: LegendItem[] = [
    { label: "Succeeded (below 400)", tone: "primary" },
    { label: "Client errors (4xx)", tone: "warning" },
    { label: "Server errors (5xx)", tone: "danger" },
  ];
  return {
    kind: "columns",
    id: "requests",
    title: `Requests per ${perText(context.bucketMinutes)}`,
    description:
      `${pluralize(summary.requests, "request")} in the window ${context.windowText}, ` +
      `${pluralize(summary.r4xx, "client error")} and ${pluralize(summary.r5xx, "server error")}. ` +
      "The table below the chart has every value.",
    legend,
    tones: legend.map((item) => item.tone),
    labels: timeLabels(context),
    tips: buckets.map((bucket, index) => {
      const segments = columns[index];
      const time = bucketTime(bucket, context);
      if (segments === null || segments === undefined) {
        return `${time}: ${NO_DATA_TEXT}`;
      }
      const total = segments.reduce((sum, value) => sum + value, 0);
      return `${time}: ${pluralize(total, "request")}, ${pluralize(bucket.r4xx, "client error")}, ${pluralize(bucket.r5xx, "server error")}`;
    }),
    maxTicks: REQUEST_LABELS_MAX_TICKS,
    xCaption: "Time (UTC)",
    columns,
    highlights: [],
    table: {
      headers: ["Time", ...legend.map((item) => item.label), "Total"],
      rows: buckets.map((bucket, index) => {
        const segments = columns[index];
        const time = rowTime(bucket, context);
        const cells =
          segments === null || segments === undefined
            ? noDataCells(legend.length + 1)
            : [
                ...segments.map((value) => formatCount(value)),
                formatCount(segments.reduce((sum, value) => sum + value, 0)),
              ];
        return { key: String(bucket.startMs), cells: [time, ...cells] };
      }),
    },
    emptyMessage: summary.requests === 0 ? "No requests in this window." : null,
  };
}

function latencyChart(context: ChartContext): ColumnChartSpec {
  const { summary } = context;
  const histogram = summary.latHist;
  const total = histogram.reduce((sum, count) => sum + count, 0);
  const p50 = percentileBucket(histogram, MEDIAN_PERCENT);
  const p95 = percentileBucket(histogram, TAIL_PERCENT);

  const highlights: { index: number; label: string }[] = [];
  if (p50 !== null && p50 === p95) {
    highlights.push({ index: p50, label: "p50, p95" });
  } else {
    if (p50 !== null) highlights.push({ index: p50, label: "p50" });
    if (p95 !== null) highlights.push({ index: p95, label: "p95" });
  }

  return {
    kind: "columns",
    id: "latency",
    title: "Response time of requests",
    description:
      `Requests by response time over the window. Half were ${describeLatency(p50)}, 95 of 100 were ${describeLatency(p95)}. ` +
      "The table below the chart has every value.",
    legend: [],
    tones: ["primary"],
    labels: histogram.map((_, index) => latencyBucketLabel(index)),
    tips: histogram.map(
      (count, index) =>
        `${latencyRangeText(index)}: ${pluralize(count, "request")} (${shareText(count, total)})`,
    ),
    maxTicks: LATENCY_MAX_TICKS,
    xCaption: "Response time in ms (upper bound of each bucket)",
    columns: histogram.map((count) => [count]),
    highlights,
    table: {
      headers: ["Response time", "Requests", "Share"],
      rows: histogram.map((count, index) => ({
        key: String(index),
        cells: [
          latencyRangeText(index),
          formatCount(count),
          shareText(count, total),
        ],
      })),
    },
    emptyMessage:
      total === 0 ? "No request was measured in this window." : null,
  };
}

function memoryChart(context: ChartContext): LineChartSpec {
  const { buckets, summary } = context;
  const isPlatformMemory = usesPlatformMemory(summary, context.isPlatform);
  const values = buckets.map((bucket) => {
    if (!bucket.hasData) return null;
    return isPlatformMemory ? bucket.memUsedMbMax : bucket.rssMbMax;
  });
  const limit =
    isPlatformMemory && summary.memLimitMb !== null && summary.memLimitMb > 0
      ? summary.memLimitMb
      : null;
  const peak = values.reduce<number>(
    (highest, value) => (value === null ? highest : Math.max(highest, value)),
    0,
  );
  const hasValues = values.some((value) => value !== null);
  const tone: ChartTone = "primary";
  const referenceTone: ChartTone = "danger";
  const legend: LegendItem[] =
    limit === null
      ? []
      : [
          { label: "Memory used", tone },
          { label: "Memory limit", tone: referenceTone },
        ];
  const headers = [
    "Time",
    "Memory (MB)",
    ...(limit === null ? [] : ["Limit (MB)"]),
  ];

  return {
    kind: "line",
    id: "memory",
    title: `Memory peak per ${perText(context.bucketMinutes)}`,
    description:
      `Highest memory ${formatMegabytes(peak)} in the window` +
      (limit === null
        ? "."
        : `, against a limit of ${formatMegabytes(limit)}.`) +
      " The table below the chart has every value.",
    legend,
    labels: timeLabels(context),
    tips: buckets.map((bucket, index) => {
      const value = values[index];
      const time = bucketTime(bucket, context);
      return value === null || value === undefined
        ? `${time}: ${NO_DATA_TEXT}`
        : `${time}: ${formatMegabytes(value)}`;
    }),
    maxTicks: REQUEST_LABELS_MAX_TICKS,
    xCaption: "Time (UTC)",
    tone,
    values,
    reference: limit,
    referenceLabel: limit === null ? null : "Memory limit",
    referenceTone,
    table: {
      headers,
      rows: buckets.map((bucket, index) => {
        const value = values[index];
        const cells =
          value === null || value === undefined
            ? noDataCells(headers.length - 1)
            : [
                formatCount(value),
                ...(limit === null ? [] : [formatCount(limit)]),
              ];
        return {
          key: String(bucket.startMs),
          cells: [rowTime(bucket, context), ...cells],
        };
      }),
    },
    emptyMessage: hasValues ? null : "No memory figure in this window.",
  };
}

function egressChart(context: ChartContext): ColumnChartSpec {
  const { buckets, summary } = context;
  return {
    kind: "columns",
    id: "egress",
    title: `Egress drops per ${perText(context.bucketMinutes)}`,
    description:
      `${pluralize(summary.egressDropped, "packet")} dropped by egress rules in the window. ` +
      "The table below the chart has every value.",
    legend: [],
    tones: ["warning"],
    labels: timeLabels(context),
    tips: buckets.map((bucket) => {
      const time = bucketTime(bucket, context);
      return bucket.hasData
        ? `${time}: ${pluralize(bucket.egressDropped, "drop")}`
        : `${time}: ${NO_DATA_TEXT}`;
    }),
    maxTicks: REQUEST_LABELS_MAX_TICKS,
    xCaption: "Time (UTC)",
    columns: buckets.map((bucket) =>
      bucket.hasData ? [bucket.egressDropped] : null,
    ),
    highlights: [],
    table: {
      headers: ["Time", "Drops"],
      rows: buckets.map((bucket) => ({
        key: String(bucket.startMs),
        cells: [
          rowTime(bucket, context),
          bucket.hasData ? formatCount(bucket.egressDropped) : NO_DATA_TEXT,
        ],
      })),
    },
    emptyMessage:
      summary.egressDropped === 0
        ? "No packet was dropped in this window."
        : null,
  };
}

// ------------------------------------------------------------ comparison

export interface ComparisonCells {
  requests: string;
  serverErrors: string;
  p50: string;
  p95: string;
  memory: string;
  egressDrops: string;
  restarts: string;
}

/** One row of the comparison table as text. */
export function comparisonCells(row: AppComparisonRow): ComparisonCells {
  return {
    requests: formatCount(row.requests),
    serverErrors: formatCount(row.r5xx),
    p50: describeLatency(row.p50Bucket),
    p95: describeLatency(row.p95Bucket),
    memory: formatMegabytes(row.rssMbMax),
    egressDrops: formatCount(row.egressDropped),
    restarts: formatCount(row.restarts),
  };
}
