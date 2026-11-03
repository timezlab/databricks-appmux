/**
 * Everything the dashboard and an app's Insights show of one series (the
 * platform row or one app), as plain text and numbers: the answer of
 * `GET /metrics` and the scope go in; the figures, chart specs (with the
 * tooltip texts and the table twin of each), notices and fact lists come out.
 * The components copy these into elements and add no rule of their own, so the
 * wording and the arithmetic are tested here without a browser.
 *
 * Every string that comes from the API (a slug, a rule id) is passed on as text;
 * nothing here builds markup or a link.
 */
import type { MetricsResponse } from "@/lib/api/schemas";
import { clockLabel } from "./chart";
import { buildFigures, shareText } from "./figures";
import type { Figure, PreviousWindow } from "./figures";
import { PLATFORM_APP_SLUG } from "./scope";
import type { DashboardScope } from "./scope";
import {
  LATENCY_BUCKET_BOUNDS_MS,
  MINUTE_MS,
  bucketRows,
  describeLatency,
  dropsByRule,
  latencyBucketLabel,
  lengthText,
  perText,
  percentileBucket,
  rowsInDomain,
  stepText,
  summarizeRows,
  windowDomain,
} from "./series";
import type {
  BucketSlot,
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
  pluralize,
} from "@/lib/format";

// ------------------------------------------------------------------ types

/**
 * Which series colour a mark takes; `series-style.ts` maps each tone to a
 * `--chart-N` token. `primary`, `warning` and `danger` are the parts of one
 * series (requests that succeeded, client errors, server errors);
 * `series-1` to `series-8` are the apps of the breakdown, in a fixed order, and
 * `other` is every app below the cut.
 */
export type ChartTone =
  | "primary"
  | "warning"
  | "danger"
  | "series-1"
  | "series-2"
  | "series-3"
  | "series-4"
  | "series-5"
  | "series-6"
  | "series-7"
  | "series-8"
  | "other";

/**
 * The pattern laid over a mark (and its legend swatch) besides its colour. The
 * tones differ little in lightness, so a person who cannot tell the hues apart
 * still tells a plain segment from a striped one from a dotted one. The app
 * tones alternate plain and striped, so two neighbours in a stack always differ
 * in pattern; the six tokens fail the colour-blind check between some
 * neighbours (`--chart-2` and `--chart-3` in dark mode), and the pattern is
 * what tells those apart. Apps 7 and 8 reuse the first two hues (7 dotted, 8
 * plain), and the grey of `other`, close in lightness to some hues, is dotted.
 */
export type ToneTexture = "none" | "stripes" | "dots";

export const TONE_TEXTURE: Record<ChartTone, ToneTexture> = {
  primary: "none",
  warning: "stripes",
  danger: "dots",
  "series-1": "none",
  "series-2": "stripes",
  "series-3": "none",
  "series-4": "stripes",
  "series-5": "none",
  "series-6": "stripes",
  "series-7": "dots",
  "series-8": "none",
  other: "dots",
};

export interface LegendItem {
  label: string;
  tone: ChartTone;
  /** An app's slug: the legend shows its route strip and links to its Insights. */
  slug?: string;
}

/** The accessible twin of a chart: the first cell of each row names the row. */
export interface ChartTable {
  headers: string[];
  rows: { key: string; cells: string[] }[];
}

interface ChartBase {
  /** Unique on the page: it names the chart's texture patterns. */
  id: string;
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
  /** Text under the axis: what one column holds ("5-minute totals") and the time zone. */
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
  figures: Figure[];
  charts: ChartSpec[];
  drops: RuleDrops[];
  facts: FactRow[];
}

export type DashboardModel = EmptyModel | SeriesModel;

export interface ModelOptions {
  /** The step the rows were read at, in minutes (1, 5 or 15). */
  stepMinutes?: number;
  /** The answer for the window before, for the change figures; `null` when it was not read. */
  previous?: MetricsResponse | null;
  /** The range as in "the previous hour". */
  span?: string;
}

/** A chart before its answer: what its frame is called while it loads, or when the answer could not be read. */
export interface ChartOutline {
  id: string;
  name: string;
}

/**
 * The charts `buildDashboardModel` draws for a scope, in order, by a name that
 * holds whatever the step (the titles add it: "Requests per 5 minutes"). The
 * frames take their place before the answer, so the page does not jump.
 */
export function chartOutline(scope: DashboardScope): ChartOutline[] {
  const outline: ChartOutline[] = [
    { id: "requests", name: "Requests" },
    { id: "latency", name: "Response time of requests" },
    { id: "memory", name: "Memory peak" },
    { id: "egress", name: "Egress drops" },
  ];
  if (scope.kind === "platform") {
    outline.push({ id: "apps-running", name: "Apps running" });
  }
  return outline;
}

// ----------------------------------------------------------------- tuning

/** The most time labels under a chart of the window. */
export const REQUEST_LABELS_MAX_TICKS = 6;
/** The latency chart has 11 columns of about 50 px; every one gets its label. */
const LATENCY_MAX_TICKS = 11;
const MEDIAN_PERCENT = 50;
const TAIL_PERCENT = 95;
const P99_PERCENT = 99;

// --------------------------------------------------------------- wording

export const NO_DATA_TEXT = "no data";

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

function scopeSlug(scope: DashboardScope): string {
  return scope.kind === "platform" ? PLATFORM_APP_SLUG : scope.slug;
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
  options: ModelOptions = {},
): DashboardModel {
  const stepMinutes = options.stepMinutes ?? 1;
  const base: ModelBase = {
    windowText: `${formatAbsoluteTime(response.from)} to ${formatAbsoluteTime(response.to)}`,
    notices: response.truncated ? [truncatedNotice(response)] : [],
  };
  const domain = windowDomain(response.from, response.to);
  if (domain === null) return emptyModel(base, scope, false);

  const slug = scopeSlug(scope);
  const rows = rowsInDomain(response.items, domain, stepMinutes).filter(
    (row) => row.app_slug === slug,
  );
  if (rows.length === 0) return emptyModel(base, scope, true);

  const summary = summarizeRows(rows, stepMinutes);
  const notices =
    summary.gapMinutes > 0
      ? [...base.notices, gapNotice(summary.gapMinutes, scope)]
      : base.notices;
  const { bucketMinutes, buckets } = bucketRows(
    rows,
    domain,
    undefined,
    stepMinutes,
  );
  const isPlatform = scope.kind === "platform";
  const context: ChartContext = {
    domainStartMs: domain.startMs,
    bucketMinutes,
    buckets,
    summary,
    windowText: base.windowText,
    isPlatform,
  };
  const charts: ChartSpec[] = [
    requestsChart(context),
    latencyChart(context),
    memoryChart(context),
    egressChart(context),
  ];
  if (isPlatform) charts.push(appsRunningChart(context));

  return {
    ...base,
    notices,
    kind: "series",
    figures: buildFigures({
      summary,
      previous: previousWindow(options.previous ?? null, slug, stepMinutes),
      buckets,
      isPlatform,
      span: options.span ?? "window",
    }),
    charts,
    drops: dropsByRule(rows),
    facts: buildFacts(summary, scope),
  };
}

/** The window before with its length, or `null` when it was not read or has no usable window. */
function previousWindow(
  response: MetricsResponse | null,
  slug: string,
  stepMinutes: number,
): PreviousWindow | null {
  if (response === null) return null;
  const domain = windowDomain(response.from, response.to);
  if (domain === null) return null;
  const rows = rowsInDomain(response.items, domain, stepMinutes).filter(
    (row) => row.app_slug === slug,
  );
  return {
    summary: summarizeRows(rows, stepMinutes),
    windowMinutes: (domain.endMs - domain.startMs) / MINUTE_MS,
  };
}

/** Memory figures of the platform rows are the edge's own; an app's are its resident memory. */
function usesPlatformMemory(summary: SeriesSummary, isPlatform: boolean) {
  return isPlatform && summary.memUsedMbMax !== null;
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

function spansTwoDays(buckets: readonly BucketSlot[]): boolean {
  const first = buckets[0];
  const last = buckets[buckets.length - 1];
  if (first === undefined || last === undefined) return false;
  return (
    new Date(first.startMs).getUTCDate() !== new Date(last.startMs).getUTCDate()
  );
}

/** What the time wording of a chart needs: where the window starts and how wide a bucket is. */
export interface BucketFrame {
  domainStartMs: number;
  bucketMinutes: number;
  buckets: readonly BucketSlot[];
}

/** The label under each column: the clock time, with the date once the chart spans two days. */
export function timeLabels(frame: BucketFrame): string[] {
  const withDate = spansTwoDays(frame.buckets);
  return frame.buckets.map((bucket) => clockLabel(bucket.startMs, withDate));
}

/** The start of a bucket as written in a tooltip; a bucket the window cuts says so. */
export function bucketTime(bucket: BucketSlot, frame: BucketFrame): string {
  const start = formatAbsoluteTime(
    Math.max(bucket.startMs, frame.domainStartMs),
  );
  if (bucket.isPartial) return `${start} (partial)`;
  return frame.bucketMinutes === 1
    ? start
    : `${start}, ${lengthText(frame.bucketMinutes)}`;
}

/** The first table cell does not repeat the length of the bucket: the title says it. */
export function rowTime(bucket: BucketSlot, frame: BucketFrame): string {
  const start = formatAbsoluteTime(
    Math.max(bucket.startMs, frame.domainStartMs),
  );
  return bucket.isPartial ? `${start} (partial)` : start;
}

/** "5-minute totals, time in UTC": what one column holds, and the time zone of the axis. */
export function totalsCaption(frame: BucketFrame): string {
  return `${stepText(frame.bucketMinutes)} totals, time in UTC`;
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
    xCaption: totalsCaption(context),
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
  const p99 = percentileBucket(histogram, P99_PERCENT);

  // Percentiles in one bucket share one label: "p50, p95".
  const labelsByIndex = new Map<number, string[]>();
  for (const [name, index] of [
    ["p50", p50],
    ["p95", p95],
    ["p99", p99],
  ] as const) {
    if (index === null) continue;
    labelsByIndex.set(index, [...(labelsByIndex.get(index) ?? []), name]);
  }
  const highlights = [...labelsByIndex.entries()].map(([index, names]) => ({
    index,
    label: names.join(", "),
  }));

  return {
    kind: "columns",
    id: "latency",
    title: "Response time of requests",
    description:
      `Requests by response time over the window. Half were ${describeLatency(p50)}, 95 of 100 were ${describeLatency(p95)}, 99 of 100 were ${describeLatency(p99)}. ` +
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
    xCaption: `${stepText(context.bucketMinutes)} peaks, time in UTC`,
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
    xCaption: totalsCaption(context),
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

/**
 * The platform's apps running against the apps registered: the newest count of
 * each bucket (a count is a gauge, as the server keeps the latest minute of a
 * step), with the newest registered count as the level line.
 */
function appsRunningChart(context: ChartContext): LineChartSpec {
  const { buckets, summary } = context;
  const values = buckets.map((bucket) =>
    bucket.hasData ? bucket.childrenRunning : null,
  );
  const registered = summary.childrenRegistered;
  const hasValues = values.some((value) => value !== null);
  const tone: ChartTone = "primary";
  const referenceTone: ChartTone = "warning";
  const legend: LegendItem[] =
    registered === null
      ? []
      : [
          { label: "Apps running", tone },
          { label: "Apps registered", tone: referenceTone },
        ];
  const registeredCell = (bucket: MetricBucket) =>
    bucket.childrenRegistered === null
      ? NO_DATA_TEXT
      : formatCount(bucket.childrenRegistered);

  return {
    kind: "line",
    id: "apps-running",
    title: "Apps running",
    description:
      (summary.childrenRunning === null || registered === null
        ? "No count of apps in the window."
        : `${formatCount(summary.childrenRunning)} of ${pluralize(registered, "registered app")} running at the end of the window.`) +
      " The table below the chart has every value.",
    legend,
    labels: timeLabels(context),
    tips: buckets.map((bucket, index) => {
      const value = values[index];
      const time = bucketTime(bucket, context);
      return value === null || value === undefined
        ? `${time}: ${NO_DATA_TEXT}`
        : `${time}: ${formatCount(value)} running of ${registeredCell(bucket)}`;
    }),
    maxTicks: REQUEST_LABELS_MAX_TICKS,
    xCaption: `Count at the end of each ${stepText(context.bucketMinutes)} bucket, time in UTC`,
    tone,
    values,
    reference: registered,
    referenceLabel: registered === null ? null : "Registered",
    referenceTone,
    table: {
      headers: ["Time", "Running", "Registered"],
      rows: buckets.map((bucket, index) => {
        const value = values[index];
        return {
          key: String(bucket.startMs),
          cells: [
            rowTime(bucket, context),
            value === null || value === undefined
              ? NO_DATA_TEXT
              : formatCount(value),
            value === null || value === undefined
              ? NO_DATA_TEXT
              : registeredCell(bucket),
          ],
        };
      }),
    },
    emptyMessage: hasValues ? null : "No count of apps in this window.",
  };
}
