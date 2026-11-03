/**
 * The platform view's breakdown by app, from `GET /metrics/breakdown`: the
 * requests, memory peak and server errors of each app as stacked columns, and a
 * table of the apps. Plain text and numbers, like model.ts; the components copy
 * them into elements and add no rule of their own.
 *
 * The server sends the busiest apps first and the rest summed as `other`. Each
 * app keeps one tone in all three charts. The apps stack in slug order, the
 * same in every column of every chart, with "Other apps" on top in a neutral
 * grey; the table keeps the server's order, busiest first.
 */
import type { MetricsBreakdown, MetricsPoint } from "@/lib/api/schemas";
import {
  NO_DATA_TEXT,
  REQUEST_LABELS_MAX_TICKS,
  bucketTime,
  perText,
  rowTime,
  timeLabels,
  totalsCaption,
} from "./model";
import type {
  BucketFrame,
  ChartOutline,
  ChartTone,
  ColumnChartSpec,
  LegendItem,
} from "./model";
import { stepMinutes } from "./scope";
import {
  LATENCY_BUCKET_COUNT,
  MINUTE_MS,
  bucketIndexOf,
  bucketLayout,
  bucketSlots,
  describeLatency,
  floorToMinute,
  percentileBucket,
  stepText,
  windowDomain,
} from "./series";
import type { BucketLayout } from "./series";
import { formatAbsoluteTime, formatCount, parseTime } from "@/lib/format";

export const OTHER_APPS_LABEL = "Other apps";

/** One tone per app, in stacking order. The page asks for 8 apps; any more are added to "Other apps", never given a recycled tone. */
const APP_TONES = [
  "series-1",
  "series-2",
  "series-3",
  "series-4",
  "series-5",
  "series-6",
  "series-7",
  "series-8",
] as const satisfies readonly ChartTone[];

const TAIL_PERCENT = 95;

// ----------------------------------------------------------------- types

export interface BreakdownApp {
  /** The app's slug, for its route strip and the link to its Insights; `null` for the apps below the cut. */
  slug: string | null;
  label: string;
  tone: ChartTone;
  /** The cells after the app's name, in the order of `appHeaders`. */
  cells: string[];
}

export interface BreakdownModel {
  kind: "breakdown";
  windowText: string;
  charts: ColumnChartSpec[];
  /** The headers of the apps table; the first names the app. */
  appHeaders: string[];
  /** Busiest first, the apps below the cut last. */
  apps: BreakdownApp[];
}

export interface EmptyBreakdown {
  kind: "empty";
  windowText: string;
  message: string;
}

export type BreakdownView = BreakdownModel | EmptyBreakdown;

/** The charts `buildBreakdownModel` draws, in order, by a name that holds whatever the step: the frames before the answer. */
export const BREAKDOWN_OUTLINE: readonly ChartOutline[] = [
  { id: "requests-by-app", name: "Requests by app" },
  { id: "memory-by-app", name: "Memory peak by app" },
  { id: "errors-by-app", name: "Server errors by app" },
];

/** One app's points added up over one bucket, or over the window; memory is the peak. */
interface PointTotals {
  requests: number;
  r4xx: number;
  r5xx: number;
  rssMbMax: number;
  egressDropped: number;
  latHist: number[];
}

/** One stack of the charts: an app, or the apps below the cut. */
interface Entry {
  slug: string | null;
  label: string;
  tone: ChartTone;
  /** Where the server put the app, busiest first; the apps below the cut come last. */
  rank: number;
  /** Per bucket, `null` when the app has no point in it. */
  buckets: (PointTotals | null)[];
  total: PointTotals;
}

// ----------------------------------------------------------------- sums

function emptyTotals(): PointTotals {
  return {
    requests: 0,
    r4xx: 0,
    r5xx: 0,
    rssMbMax: 0,
    egressDropped: 0,
    latHist: new Array<number>(LATENCY_BUCKET_COUNT).fill(0),
  };
}

/** Adds `point` into `target`; memory keeps the higher peak. */
function addInto(target: PointTotals, point: PointTotals): void {
  target.requests += point.requests;
  target.r4xx += point.r4xx;
  target.r5xx += point.r5xx;
  target.rssMbMax = Math.max(target.rssMbMax, point.rssMbMax);
  target.egressDropped += point.egressDropped;
  for (const [index, count] of point.latHist.entries()) {
    target.latHist[index] = (target.latHist[index] ?? 0) + count;
  }
}

function totalsOf(point: MetricsPoint): PointTotals {
  return {
    requests: point.requests,
    r4xx: point.r4xx,
    r5xx: point.r5xx,
    rssMbMax: point.rss_mb_max,
    egressDropped: point.egress_dropped,
    latHist: [...point.lat_hist],
  };
}

/**
 * The points of one stack by bucket. A step the window cuts at its start lies
 * in the first bucket; a step wholly outside the window is left out.
 */
function bucketPoints(
  points: readonly { ms: number; totals: PointTotals }[],
  layout: BucketLayout,
  stepMs: number,
): (PointTotals | null)[] {
  const buckets: (PointTotals | null)[] = new Array<PointTotals | null>(
    layout.count,
  ).fill(null);
  for (const { ms, totals } of points) {
    if (ms <= layout.domain.startMs - stepMs) continue;
    const index = bucketIndexOf(layout, ms);
    if (index === null) continue;
    const bucket = buckets[index] ?? emptyTotals();
    addInto(bucket, totals);
    buckets[index] = bucket;
  }
  return buckets;
}

function timedPoints(
  points: readonly MetricsPoint[],
): { ms: number; totals: PointTotals }[] {
  const timed: { ms: number; totals: PointTotals }[] = [];
  for (const point of points) {
    const ms = parseTime(point.minute);
    if (ms !== null)
      timed.push({ ms: floorToMinute(ms), totals: totalsOf(point) });
  }
  return timed;
}

/**
 * The apps below the cut as one list of steps: several apps in one step are
 * added up, their memory peaks too (as the server sums them for `other`).
 */
function otherPoints(
  other: readonly MetricsPoint[],
  folded: readonly MetricsPoint[],
): { ms: number; totals: PointTotals }[] {
  const byStep = new Map<number, PointTotals>();
  for (const { ms, totals } of timedPoints([...other, ...folded])) {
    const step = byStep.get(ms);
    if (step === undefined) {
      byStep.set(ms, totals);
    } else {
      const memory = step.rssMbMax + totals.rssMbMax;
      addInto(step, totals);
      step.rssMbMax = memory;
    }
  }
  return [...byStep.entries()].map(([ms, totals]) => ({ ms, totals }));
}

function windowTotal(buckets: readonly (PointTotals | null)[]): PointTotals {
  const total = emptyTotals();
  for (const bucket of buckets) {
    if (bucket !== null) addInto(total, bucket);
  }
  return total;
}

// ---------------------------------------------------------------- charts

interface StackedChart {
  id: string;
  title: string;
  /** What the columns add up, for the description: "requests". */
  noun: string;
  pick: (totals: PointTotals) => number;
  /** "120" or "412 MB": a value as the tooltip and the table write it. */
  valueText: (value: number) => string;
  /** The last line of a tooltip: "Total: 1,210". */
  totalLabel: string;
  xCaption: string;
  tableHeader: (label: string) => string;
  emptyMessage: string | null;
}

function legendOf(entries: readonly Entry[]): LegendItem[] {
  return entries.map((entry) =>
    entry.slug === null
      ? { label: entry.label, tone: entry.tone }
      : { label: entry.label, tone: entry.tone, slug: entry.slug },
  );
}

function stackedChart(
  chart: StackedChart,
  entries: readonly Entry[],
  frame: BucketFrame,
  windowText: string,
): ColumnChartSpec {
  const columns = frame.buckets.map((_, index) =>
    entries.every((entry) => entry.buckets[index] === null)
      ? null
      : entries.map((entry) => {
          const bucket = entry.buckets[index];
          return bucket === null || bucket === undefined
            ? 0
            : chart.pick(bucket);
        }),
  );
  const legend = legendOf(entries);
  const windowSum = entries.reduce(
    (sum, entry) => sum + chart.pick(entry.total),
    0,
  );

  return {
    kind: "columns",
    id: chart.id,
    title: chart.title,
    description:
      `${chart.noun} of each app in the window ${windowText}, stacked one app on another. ` +
      "The table below the chart has every value.",
    legend,
    tones: entries.map((entry) => entry.tone),
    labels: timeLabels(frame),
    // Top of the stack first, as the column reads; an app with nothing in the bucket is left out.
    tips: frame.buckets.map((slot, index) => {
      const time = bucketTime(slot, frame);
      const segments = columns[index];
      if (segments === null || segments === undefined) {
        return `${time}: ${NO_DATA_TEXT}`;
      }
      const lines = entries
        .map((entry, at) => ({ entry, value: segments[at] ?? 0 }))
        .filter(({ value }) => value > 0)
        .toReversed()
        .map(({ entry, value }) => `${entry.label}: ${chart.valueText(value)}`);
      const total = segments.reduce((sum, value) => sum + value, 0);
      return [
        time,
        ...lines,
        `${chart.totalLabel}: ${chart.valueText(total)}`,
      ].join("\n");
    }),
    maxTicks: REQUEST_LABELS_MAX_TICKS,
    xCaption: chart.xCaption,
    columns,
    highlights: [],
    table: {
      headers: [
        "Time",
        ...entries.map((entry) => chart.tableHeader(entry.label)),
        chart.totalLabel,
      ],
      rows: frame.buckets.map((slot, index) => {
        const segments = columns[index];
        const cells =
          segments === null || segments === undefined
            ? new Array<string>(entries.length + 1).fill(NO_DATA_TEXT)
            : [
                ...entries.map((entry, at) =>
                  entry.buckets[index] === null
                    ? NO_DATA_TEXT
                    : formatCount(segments[at] ?? 0),
                ),
                formatCount(segments.reduce((sum, value) => sum + value, 0)),
              ];
        return {
          key: String(slot.startMs),
          cells: [rowTime(slot, frame), ...cells],
        };
      }),
    },
    emptyMessage: windowSum > 0 ? null : chart.emptyMessage,
  };
}

// ----------------------------------------------------------------- build

function compareSlugs(a: string, b: string): number {
  if (a < b) return -1;
  return a > b ? 1 : 0;
}

export function buildBreakdownModel(response: MetricsBreakdown): BreakdownView {
  const windowText = `${formatAbsoluteTime(response.from)} to ${formatAbsoluteTime(response.to)}`;
  const domain = windowDomain(response.from, response.to);
  if (domain === null) {
    return {
      kind: "empty",
      windowText,
      message: "The answer carries no usable time window.",
    };
  }
  const kept = response.series.slice(0, APP_TONES.length);
  const folded = response.series
    .slice(APP_TONES.length)
    .flatMap((series) => series.points);
  if (kept.length === 0 && response.other === null) {
    return {
      kind: "empty",
      windowText,
      message: "No app wrote metrics in this window.",
    };
  }

  const stepMs = stepMinutes(response.step) * MINUTE_MS;
  const layout = bucketLayout(domain, undefined, stepMinutes(response.step));
  const frame: BucketFrame = {
    domainStartMs: domain.startMs,
    bucketMinutes: layout.bucketMinutes,
    buckets: bucketSlots(layout),
  };

  const appEntries = kept
    .map((series, rank) => ({ series, rank }))
    .toSorted((a, b) => compareSlugs(a.series.app_slug, b.series.app_slug))
    .map(({ series, rank }, index): Entry => {
      const buckets = bucketPoints(timedPoints(series.points), layout, stepMs);
      return {
        slug: series.app_slug,
        label: series.display_name ?? series.app_slug,
        tone: APP_TONES[index] ?? "other",
        rank,
        buckets,
        total: windowTotal(buckets),
      };
    });
  const entries = [...appEntries];
  if (response.other !== null || folded.length > 0) {
    const buckets = bucketPoints(
      otherPoints(response.other ?? [], folded),
      layout,
      stepMs,
    );
    entries.push({
      slug: null,
      label: OTHER_APPS_LABEL,
      tone: "other",
      rank: kept.length,
      buckets,
      total: windowTotal(buckets),
    });
  }

  const per = perText(layout.bucketMinutes);
  const charts = [
    stackedChart(
      {
        id: "requests-by-app",
        title: `Requests by app, per ${per}`,
        noun: "Requests",
        pick: (totals) => totals.requests,
        valueText: formatCount,
        totalLabel: "Total",
        xCaption: totalsCaption(frame),
        tableHeader: (label) => label,
        emptyMessage: "No requests in this window.",
      },
      entries,
      frame,
      windowText,
    ),
    stackedChart(
      {
        id: "memory-by-app",
        title: `Memory peak by app, per ${per}`,
        noun: "The memory peak",
        pick: (totals) => totals.rssMbMax,
        valueText: (value) => `${formatCount(value)} MB`,
        totalLabel: "Sum of peaks",
        xCaption: `Each app's ${stepText(layout.bucketMinutes)} peak, stacked; time in UTC`,
        tableHeader: (label) => `${label} (MB)`,
        emptyMessage: "No memory figure in this window.",
      },
      entries,
      frame,
      windowText,
    ),
    stackedChart(
      {
        id: "errors-by-app",
        title: `Server errors by app, per ${per}`,
        noun: "Server errors (5xx)",
        pick: (totals) => totals.r5xx,
        valueText: formatCount,
        totalLabel: "Total",
        xCaption: totalsCaption(frame),
        tableHeader: (label) => label,
        emptyMessage: "No server error in this window.",
      },
      entries,
      frame,
      windowText,
    ),
  ];

  const apps = entries
    .toSorted((a, b) => a.rank - b.rank)
    .map((entry): BreakdownApp => ({
      slug: entry.slug,
      label: entry.label,
      tone: entry.tone,
      cells: [
        formatCount(entry.total.requests),
        formatCount(entry.total.r4xx),
        formatCount(entry.total.r5xx),
        describeLatency(percentileBucket(entry.total.latHist, TAIL_PERCENT)),
        entry.buckets.every((bucket) => bucket === null)
          ? NO_DATA_TEXT
          : `${formatCount(entry.total.rssMbMax)} MB`,
        formatCount(entry.total.egressDropped),
      ],
    }));

  return {
    kind: "breakdown",
    windowText,
    charts,
    appHeaders: [
      "App",
      "Requests",
      "Client errors (4xx)",
      "Server errors (5xx)",
      "Response time, p95",
      "Memory peak",
      "Egress drops",
    ],
    apps,
  };
}
