/**
 * Turns the rows of `GET /metrics` into what the dashboard shows: buckets on
 * UTC boundaries, as many as a chart has columns (one per minute or per step
 * while they fit, wider ones for a long window), sums and peaks over the
 * window, and percentiles read from the latency histogram. Pure functions over
 * the `MetricsRow` of the contract (admin-api.md, design 10.2, data model 4.8).
 * A row is one step (1, 5 or 15 minutes, `minute` its start); a step the window
 * cuts keeps its start, so it may begin before the window.
 *
 * Rows of one series (one app, or `_platform`) go in; mixing series in one call
 * would add them together.
 */
import type { MetricsRow } from "@/lib/api/schemas";
import { MAX_WINDOW_MINUTES } from "./scope";
import { formatCount, formatDurationMs, parseTime } from "@/lib/format";

export const MINUTE_MS = 60_000;

// ---------------------------------------------------------------- latency

/** Upper bounds of the first 10 latency buckets, in ms (design 10.2); the 11th counts everything slower. */
export const LATENCY_BUCKET_BOUNDS_MS = [
  5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000,
] as const;
export const LATENCY_BUCKET_COUNT = LATENCY_BUCKET_BOUNDS_MS.length + 1;
const SLOWEST_BOUND_MS = 5000;

/** "5", "1,000", "> 5,000": the bucket's upper bound in ms, as a chart axis label. */
export function latencyBucketLabel(index: number): string {
  const bound = LATENCY_BUCKET_BOUNDS_MS[index];
  return bound === undefined
    ? `> ${formatCount(SLOWEST_BOUND_MS)}`
    : formatCount(bound);
}

/**
 * "at most 25 ms": a percentile is the upper bound of the bucket it falls in
 * (spec US11, design 10.2), so it is coarse by design and never a made-up
 * exact number. The last bucket has no bound: "over 5 s".
 */
export function describeLatency(bucketIndex: number | null): string {
  if (bucketIndex === null) return "no requests";
  const bound = LATENCY_BUCKET_BOUNDS_MS[bucketIndex];
  return bound === undefined
    ? `over ${formatDurationMs(SLOWEST_BOUND_MS)}`
    : `at most ${formatDurationMs(bound)}`;
}

/**
 * The bucket that holds the request at `percentile` (1 to 100) of the
 * histogram's total, rank rounded up: 412 requests and the p50 is the 206th
 * (data model 4.8's worked example). `null` when nothing was measured. Several
 * minutes are added bucket by bucket first; percentiles of minutes are never
 * averaged (design 10.2).
 */
export function percentileBucket(
  histogram: readonly number[],
  percentile: number,
): number | null {
  if (!(percentile >= 1 && percentile <= 100)) {
    throw new RangeError(`percentile ${percentile} is outside 1 to 100`);
  }
  const counts = histogram.map((count) => (count > 0 ? count : 0));
  const total = counts.reduce((sum, count) => sum + count, 0);
  if (total <= 0) return null;
  // Multiply before dividing: 100 requests at the p95 is exactly rank 95.
  const rank = Math.max(1, Math.ceil((total * percentile) / 100));
  let running = 0;
  for (const [index, count] of counts.entries()) {
    running += count;
    if (running >= rank) return index;
  }
  return null;
}

// ----------------------------------------------------------------- domain

export function floorToMinute(ms: number): number {
  return Math.floor(ms / MINUTE_MS) * MINUTE_MS;
}

export function ceilToMinute(ms: number): number {
  return Math.ceil(ms / MINUTE_MS) * MINUTE_MS;
}

/** The minutes a chart covers: from the start of the first to the end of the last. Both are whole minutes. */
export interface Domain {
  startMs: number;
  endMs: number;
}

/**
 * The window the answer says it applied (`from` moves forward when the answer
 * was cut, so this is the window to draw, not the one asked for). `null` when
 * the answer has no usable window. A window longer than the API allows (7 days)
 * keeps its newest minutes.
 */
export function windowDomain(from: string, to: string): Domain | null {
  const fromMs = parseTime(from);
  const toMs = parseTime(to);
  if (fromMs === null || toMs === null) return null;
  let startMs = floorToMinute(fromMs);
  const endMs = ceilToMinute(toMs);
  if (endMs <= startMs) return null;
  const longest = MAX_WINDOW_MINUTES * MINUTE_MS;
  if (endMs - startMs > longest) startMs = endMs - longest;
  return { startMs, endMs };
}

function rowMinuteMs(row: MetricsRow): number | null {
  const ms = parseTime(row.minute);
  return ms === null ? null : floorToMinute(ms);
}

/**
 * The rows whose step reaches into the domain, oldest first: a step that
 * starts before the window but ends inside it is the cut first step.
 */
export function rowsInDomain(
  rows: readonly MetricsRow[],
  domain: Domain,
  stepMinutes = 1,
): MetricsRow[] {
  const firstMs = domain.startMs - stepMinutes * MINUTE_MS;
  const inside: { row: MetricsRow; ms: number }[] = [];
  for (const row of rows) {
    const ms = rowMinuteMs(row);
    if (ms !== null && ms > firstMs && ms < domain.endMs) {
      inside.push({ row, ms });
    }
  }
  return inside.toSorted((a, b) => a.ms - b.ms).map((entry) => entry.row);
}

// --------------------------------------------------------------- buckets

/**
 * Bucket widths, in minutes, a chart may use. Each divides a day, so a bucket
 * on a UTC boundary holds whole steps of 1, 5 or 15 minutes when its width is a
 * multiple of the step.
 */
export const BUCKET_MINUTE_CHOICES = [
  1, 2, 5, 10, 15, 20, 30, 60, 120, 180, 360, 720, 1440,
] as const;
/** The most columns a chart draws: a column stays about 7 px wide in the 512 px chart. */
export const MAX_COLUMNS = 72;

/** Where the buckets of a domain lie: `count` buckets of `bucketMinutes`, the first starting at `firstStartMs`. */
export interface BucketLayout {
  domain: Domain;
  bucketMinutes: number;
  firstStartMs: number;
  count: number;
}

function layoutFor(domain: Domain, bucketMinutes: number): BucketLayout {
  const bucketMs = bucketMinutes * MINUTE_MS;
  const firstStartMs = Math.floor(domain.startMs / bucketMs) * bucketMs;
  const endMs = Math.ceil(domain.endMs / bucketMs) * bucketMs;
  return {
    domain,
    bucketMinutes,
    firstStartMs,
    count: Math.round((endMs - firstStartMs) / bucketMs),
  };
}

/**
 * The narrowest width, a whole number of steps, whose buckets on UTC
 * boundaries number at most `maxColumns`: one per minute (or per step) while
 * they fit.
 */
export function chooseBucketMinutes(
  domain: Domain,
  maxColumns: number = MAX_COLUMNS,
  stepMinutes = 1,
): number {
  const choices = BUCKET_MINUTE_CHOICES.filter(
    (size) => size % stepMinutes === 0,
  );
  for (const size of choices) {
    if (layoutFor(domain, size).count <= maxColumns) return size;
  }
  return choices[choices.length - 1] ?? BUCKET_MINUTE_CHOICES[0];
}

/** The buckets of a domain for a chart of at most `maxColumns`. */
export function bucketLayout(
  domain: Domain,
  maxColumns: number = MAX_COLUMNS,
  stepMinutes = 1,
): BucketLayout {
  return layoutFor(
    domain,
    chooseBucketMinutes(domain, maxColumns, stepMinutes),
  );
}

/**
 * The bucket a row of `minuteMs` belongs to, or `null` outside the domain. A
 * step the window cuts at its start lies in the first bucket: buckets hold
 * whole steps.
 */
export function bucketIndexOf(
  layout: BucketLayout,
  minuteMs: number,
): number | null {
  if (minuteMs < layout.firstStartMs || minuteMs >= layout.domain.endMs) {
    return null;
  }
  const index = Math.floor(
    (minuteMs - layout.firstStartMs) / (layout.bucketMinutes * MINUTE_MS),
  );
  return index < layout.count ? index : null;
}

/** Where one bucket lies against the window. */
export interface BucketSlot {
  /** Start of the bucket, on a UTC boundary; the first may start before the window. */
  startMs: number;
  /** Minutes of the bucket that lie inside the window. */
  coveredMinutes: number;
  /** The window cuts the bucket: the first starts before it, or the newest is still filling. */
  isPartial: boolean;
}

export function bucketSlots(layout: BucketLayout): BucketSlot[] {
  const bucketMs = layout.bucketMinutes * MINUTE_MS;
  return Array.from({ length: layout.count }, (_, index) => {
    const startMs = layout.firstStartMs + index * bucketMs;
    const coveredMs =
      Math.min(startMs + bucketMs, layout.domain.endMs) -
      Math.max(startMs, layout.domain.startMs);
    const coveredMinutes = Math.round(coveredMs / MINUTE_MS);
    return {
      startMs,
      coveredMinutes,
      isPartial: coveredMinutes < layout.bucketMinutes,
    };
  });
}

const MINUTES_PER_HOUR = 60;
const MINUTES_PER_DAY = 1440;

/** "5-minute", "3-hour", "1-day": the width of a bucket, as in "5-minute totals". */
export function stepText(minutes: number): string {
  if (minutes % MINUTES_PER_DAY === 0) {
    return `${minutes / MINUTES_PER_DAY}-day`;
  }
  if (minutes % MINUTES_PER_HOUR === 0) {
    return `${minutes / MINUTES_PER_HOUR}-hour`;
  }
  return `${minutes}-minute`;
}

/** The figures of the rows that fall in one bucket: counts added, peaks maxed. */
export interface MetricBucket extends BucketSlot {
  /** Rows found: 0 means no data (a stopped app, or a minute that was never written). */
  reportedMinutes: number;
  hasData: boolean;
  requests: number;
  r2xx: number;
  r3xx: number;
  r4xx: number;
  r5xx: number;
  upstreamErrors: number;
  limited: number;
  egressDropped: number;
  restarts: number;
  rssMbMax: number;
  /** `_platform` rows only; `null` in a bucket of an app's rows. */
  memUsedMbMax: number | null;
  memLimitMb: number | null;
  /** `_platform` rows only: the newest counts in the bucket (gauges, like the server's steps). */
  childrenRunning: number | null;
  childrenRegistered: number | null;
  latHist: number[];
}

export interface BucketedSeries {
  bucketMinutes: number;
  /** Oldest first; the last one ends where the window ends. */
  buckets: MetricBucket[];
}

function maxOrNull(current: number | null, next: number | null): number | null {
  if (next === null) return current;
  return current === null ? next : Math.max(current, next);
}

function addHistogram(target: number[], source: readonly number[]): void {
  for (let index = 0; index < target.length; index += 1) {
    target[index] = (target[index] ?? 0) + (source[index] ?? 0);
  }
}

function emptyHistogram(): number[] {
  return new Array<number>(LATENCY_BUCKET_COUNT).fill(0);
}

/**
 * One bucket per `bucketMinutes` on UTC boundaries over the domain. Minutes
 * with no row stay empty buckets (`hasData: false`): a gap is not drawn as a
 * zero. A row outside the domain is ignored.
 */
export function bucketRows(
  rows: readonly MetricsRow[],
  domain: Domain,
  maxColumns: number = MAX_COLUMNS,
  stepMinutes = 1,
): BucketedSeries {
  const layout = bucketLayout(domain, maxColumns, stepMinutes);
  const buckets: MetricBucket[] = bucketSlots(layout).map((slot) => ({
    ...slot,
    reportedMinutes: 0,
    hasData: false,
    requests: 0,
    r2xx: 0,
    r3xx: 0,
    r4xx: 0,
    r5xx: 0,
    upstreamErrors: 0,
    limited: 0,
    egressDropped: 0,
    restarts: 0,
    rssMbMax: 0,
    memUsedMbMax: null,
    memLimitMb: null,
    childrenRunning: null,
    childrenRegistered: null,
    latHist: emptyHistogram(),
  }));
  const newestChildrenMs = new Array<number>(buckets.length).fill(
    Number.NEGATIVE_INFINITY,
  );

  for (const row of rows) {
    const minuteMs = rowMinuteMs(row);
    if (minuteMs === null) continue;
    const index = bucketIndexOf(layout, minuteMs);
    if (index === null) continue;
    const bucket = buckets[index];
    if (bucket === undefined) continue;
    bucket.reportedMinutes += 1;
    bucket.hasData = true;
    bucket.requests += row.requests;
    bucket.r2xx += row.r2xx;
    bucket.r3xx += row.r3xx;
    bucket.r4xx += row.r4xx;
    bucket.r5xx += row.r5xx;
    bucket.upstreamErrors += row.upstream_errors;
    bucket.limited += row.limited;
    bucket.egressDropped += row.egress_dropped;
    bucket.restarts += row.restarts;
    bucket.rssMbMax = Math.max(bucket.rssMbMax, row.rss_mb_max);
    bucket.memUsedMbMax = maxOrNull(bucket.memUsedMbMax, row.mem_used_mb);
    bucket.memLimitMb = maxOrNull(bucket.memLimitMb, row.mem_limit_mb);
    const newestMs = newestChildrenMs[index] ?? Number.NEGATIVE_INFINITY;
    if (row.children_running !== null && minuteMs >= newestMs) {
      newestChildrenMs[index] = minuteMs;
      bucket.childrenRunning = row.children_running;
      bucket.childrenRegistered = row.children_registered;
    }
    addHistogram(bucket.latHist, row.lat_hist);
  }
  return { bucketMinutes: layout.bucketMinutes, buckets };
}

// --------------------------------------------------------------- summary

/** Totals and peaks over every row of a series. */
export interface SeriesSummary {
  reportedMinutes: number;
  firstMinuteMs: number | null;
  lastMinuteMs: number | null;
  /** Minutes of the steps with no row between the first and the last row. Steps before the first or after the last are not gaps: the app was not running yet, or its newest row is not written yet. */
  gapMinutes: number;
  requests: number;
  r2xx: number;
  r3xx: number;
  r4xx: number;
  r5xx: number;
  upstreamErrors: number;
  limited: number;
  bytesIn: number;
  bytesOut: number;
  inFlightMax: number;
  wsOpened: number;
  restarts: number;
  rssMbMax: number;
  egressDropped: number;
  egressBytesOut: number;
  egressBytesIn: number;
  latHist: number[];
  // `_platform` rows only; `null` when no row carried a value.
  memUsedMbMax: number | null;
  memLimitMb: number | null;
  /** Newest value in the window. */
  childrenRunning: number | null;
  childrenRegistered: number | null;
  admissionRejections: number | null;
  registryErrors: number | null;
  registryMsMax: number | null;
  edgeRssMbMax: number | null;
}

function addOrNull(current: number | null, next: number | null): number | null {
  if (next === null) return current;
  return (current ?? 0) + next;
}

/** Totals and peaks of the rows; each row is one step of `stepMinutes`. */
export function summarizeRows(
  rows: readonly MetricsRow[],
  stepMinutes = 1,
): SeriesSummary {
  const summary: SeriesSummary = {
    reportedMinutes: 0,
    firstMinuteMs: null,
    lastMinuteMs: null,
    gapMinutes: 0,
    requests: 0,
    r2xx: 0,
    r3xx: 0,
    r4xx: 0,
    r5xx: 0,
    upstreamErrors: 0,
    limited: 0,
    bytesIn: 0,
    bytesOut: 0,
    inFlightMax: 0,
    wsOpened: 0,
    restarts: 0,
    rssMbMax: 0,
    egressDropped: 0,
    egressBytesOut: 0,
    egressBytesIn: 0,
    latHist: emptyHistogram(),
    memUsedMbMax: null,
    memLimitMb: null,
    childrenRunning: null,
    childrenRegistered: null,
    admissionRejections: null,
    registryErrors: null,
    registryMsMax: null,
    edgeRssMbMax: null,
  };
  const minutes = new Set<number>();
  let newestChildrenMs = Number.NEGATIVE_INFINITY;

  for (const row of rows) {
    const minuteMs = rowMinuteMs(row);
    if (minuteMs === null) continue;
    minutes.add(minuteMs);
    summary.requests += row.requests;
    summary.r2xx += row.r2xx;
    summary.r3xx += row.r3xx;
    summary.r4xx += row.r4xx;
    summary.r5xx += row.r5xx;
    summary.upstreamErrors += row.upstream_errors;
    summary.limited += row.limited;
    summary.bytesIn += row.bytes_in;
    summary.bytesOut += row.bytes_out;
    summary.inFlightMax = Math.max(summary.inFlightMax, row.in_flight_max);
    summary.wsOpened += row.ws_opened;
    summary.restarts += row.restarts;
    summary.rssMbMax = Math.max(summary.rssMbMax, row.rss_mb_max);
    summary.egressDropped += row.egress_dropped;
    summary.egressBytesOut += row.egress_bytes_out;
    summary.egressBytesIn += row.egress_bytes_in;
    addHistogram(summary.latHist, row.lat_hist);
    summary.memUsedMbMax = maxOrNull(summary.memUsedMbMax, row.mem_used_mb);
    summary.memLimitMb = maxOrNull(summary.memLimitMb, row.mem_limit_mb);
    summary.admissionRejections = addOrNull(
      summary.admissionRejections,
      row.admission_rejections,
    );
    summary.registryErrors = addOrNull(
      summary.registryErrors,
      row.registry_errors,
    );
    summary.registryMsMax = maxOrNull(
      summary.registryMsMax,
      row.registry_ms_max,
    );
    summary.edgeRssMbMax = maxOrNull(summary.edgeRssMbMax, row.edge_rss_mb);
    if (row.children_running !== null && minuteMs >= newestChildrenMs) {
      newestChildrenMs = minuteMs;
      summary.childrenRunning = row.children_running;
      summary.childrenRegistered = row.children_registered;
    }
  }

  summary.reportedMinutes = minutes.size * stepMinutes;
  if (minutes.size > 0) {
    // A loop, not `Math.min(...minutes)`: a week of rows is a lot of arguments.
    let first = Number.POSITIVE_INFINITY;
    let last = Number.NEGATIVE_INFINITY;
    for (const minute of minutes) {
      first = Math.min(first, minute);
      last = Math.max(last, minute);
    }
    const stepMs = stepMinutes * MINUTE_MS;
    summary.firstMinuteMs = first;
    summary.lastMinuteMs = last;
    summary.gapMinutes =
      (Math.round((last - first) / stepMs) + 1 - minutes.size) * stepMinutes;
  }
  return summary;
}

// --------------------------------------------------------------- egress

export interface RuleDrops {
  /** The rule id as the server wrote it (`app:2`, `global:1`); shown as text. */
  rule: string;
  count: number;
}

/** Drops per rule over the rows, most first: a child probing blocked destinations shows up as one rule with many (design 10.2). */
export function dropsByRule(rows: readonly MetricsRow[]): RuleDrops[] {
  const totals = new Map<string, number>();
  for (const row of rows) {
    for (const [rule, count] of Object.entries(row.egress_dropped_by_rule)) {
      if (Number.isFinite(count) && count > 0) {
        totals.set(rule, (totals.get(rule) ?? 0) + count);
      }
    }
  }
  return [...totals.entries()]
    .map(([rule, count]) => ({ rule, count }))
    .toSorted(
      (a, b) =>
        b.count - a.count || (a.rule < b.rule ? -1 : a.rule > b.rule ? 1 : 0),
    );
}
