/**
 * The strip of figures above the charts: each figure a number with its unit, a
 * label, a short line saying what it means, a sparkline of the window and,
 * where a change means something, how it compares with the window before.
 * Plain text and numbers; the strip copies them into elements.
 */
import { formatCount } from "@/lib/format";
import {
  LATENCY_BUCKET_BOUNDS_MS,
  describeLatency,
  percentileBucket,
} from "./series";
import type { MetricBucket, SeriesSummary } from "./series";

const PERCENT = 100;
const SECOND_MS = 1000;
const SHARE_DECIMALS_BELOW_PERCENT = 10;
const SHARE_FLOOR_PERCENT = 0.1;
const MEDIAN_PERCENT = 50;
const TAIL_PERCENT = 95;
const SLOWEST_BOUND_S = 5;

// ---------------------------------------------------------------- wording

/** "0.7%", "25%", "less than 0.1%": a small share keeps its decimal, a big one is whole. */
export function shareText(part: number, whole: number): string {
  if (!(whole > 0)) return "0%";
  const percent = (part / whole) * PERCENT;
  if (percent <= 0) return "0%";
  if (percent >= SHARE_DECIMALS_BELOW_PERCENT) {
    return `${Math.round(percent)}%`;
  }
  if (percent < SHARE_FLOOR_PERCENT) return `less than ${SHARE_FLOOR_PERCENT}%`;
  return `${Number(percent.toFixed(1))}%`;
}

export interface FigureChange {
  /** Which way the figure moved; an arrow beside the words, never the words' colour. */
  direction: "up" | "down" | "same";
  text: string;
}

export interface ChangeWords {
  more: string;
  less: string;
}

const COUNT_WORDS: ChangeWords = { more: "more", less: "fewer" };

/**
 * A count against the same count in the window before: "12% more than the
 * previous hour". `null` when there is no previous window. A rise from zero
 * has no percentage, so it is said in words.
 */
export function countChange(
  current: number,
  previous: number | null,
  span: string,
  words: ChangeWords = COUNT_WORDS,
): FigureChange | null {
  if (previous === null) return null;
  const than = `the previous ${span}`;
  if (previous === 0) {
    return current === 0
      ? { direction: "same", text: `Same as ${than}` }
      : { direction: "up", text: `Up from none in ${than}` };
  }
  const percent = Math.round(((current - previous) / previous) * PERCENT);
  if (percent === 0) {
    return {
      direction: "same",
      text:
        current === previous ? `Same as ${than}` : `About the same as ${than}`,
    };
  }
  return percent > 0
    ? {
        direction: "up",
        text: `${formatCount(percent)}% ${words.more} than ${than}`,
      }
    : {
        direction: "down",
        text: `${formatCount(-percent)}% ${words.less} than ${than}`,
      };
}

/**
 * A percentile against the window before, by latency bucket: the buckets are
 * all a percentile knows, so "faster" means a lower bucket.
 */
export function latencyChange(
  currentBucket: number | null,
  previousBucket: number | null,
  span: string,
): FigureChange | null {
  if (currentBucket === null || previousBucket === null) return null;
  const than = `the previous ${span}`;
  if (currentBucket === previousBucket) {
    return { direction: "same", text: `Same as ${than}` };
  }
  const then = `(${describeLatency(previousBucket)} then)`;
  return currentBucket > previousBucket
    ? { direction: "up", text: `Slower than ${than} ${then}` }
    : { direction: "down", text: `Faster than ${than} ${then}` };
}

/** A percentile's bucket as a figure: "≤ 25" ms, "≤ 2.5" s, "> 5" s. */
export function latencyValue(bucketIndex: number | null): {
  value: string;
  unit: string | null;
} {
  if (bucketIndex === null) return { value: "None", unit: null };
  const bound = LATENCY_BUCKET_BOUNDS_MS[bucketIndex];
  if (bound === undefined) return { value: `> ${SLOWEST_BOUND_S}`, unit: "s" };
  return bound < SECOND_MS
    ? { value: `≤ ${bound}`, unit: "ms" }
    : { value: `≤ ${bound / SECOND_MS}`, unit: "s" };
}

// -------------------------------------------------------------- sparkline

/** Room left above the highest and below the lowest point, so a 2 px line is not cut. */
const SPARK_PAD = 2;

function round(value: number): number {
  return Math.round(value * 100) / 100;
}

/**
 * The `d` of a sparkline: the values left to right, zero at the bottom and the
 * highest value at the top, broken where a value is missing (a gap is not a
 * zero). A point with no neighbour is a zero-length segment, which a round
 * line cap draws as a dot.
 */
export function sparklinePath(
  values: readonly (number | null)[],
  width: number,
  height: number,
): string {
  let highest = 0;
  for (const value of values) {
    if (value !== null && value > highest) highest = value;
  }
  const span = height - SPARK_PAD * 2;
  const xAt = (index: number) =>
    values.length === 1 ? width / 2 : (index / (values.length - 1)) * width;
  const yAt = (value: number) =>
    highest === 0
      ? height - SPARK_PAD
      : height - SPARK_PAD - (Math.max(value, 0) / highest) * span;

  const parts: string[] = [];
  let runLength = 0;
  for (const [index, value] of values.entries()) {
    if (value === null) {
      if (runLength === 1) parts.push(parts.at(-1)?.replace("M", "L") ?? "");
      runLength = 0;
      continue;
    }
    const point = `${round(xAt(index))} ${round(yAt(value))}`;
    parts.push(runLength === 0 ? `M${point}` : `L${point}`);
    runLength += 1;
  }
  if (runLength === 1) parts.push(parts.at(-1)?.replace("M", "L") ?? "");
  return parts.join("");
}

// ---------------------------------------------------------------- figures

export type FigureKey =
  "requests" | "errors" | "p50" | "p95" | "memory" | "egress" | "apps";

export interface Figure {
  key: FigureKey;
  label: string;
  /** The number, ready to show: "12,345", "≤ 25". */
  value: string;
  /** "ms", "MB", "of 4"; `null` when the label says what is counted. */
  unit: string | null;
  /** One short line on what the number means. */
  detail: string | null;
  /** One value per bucket of the window, `null` where there was no data. */
  spark: (number | null)[];
  /** What the sparkline shows, for a screen reader. */
  sparkText: string;
  change: FigureChange | null;
}

export interface FiguresInput {
  summary: SeriesSummary;
  /** The window before, or `null` when it was not read (or is older than the data kept). */
  previous: SeriesSummary | null;
  buckets: readonly MetricBucket[];
  isPlatform: boolean;
  /** "hour", "6 hours": the range, as in "the previous hour". */
  span: string;
}

function sparkOf(
  buckets: readonly MetricBucket[],
  pick: (bucket: MetricBucket) => number | null,
): (number | null)[] {
  return buckets.map((bucket) => (bucket.hasData ? pick(bucket) : null));
}

function highestText(values: readonly (number | null)[], unit: string): string {
  let highest: number | null = null;
  for (const value of values) {
    if (value !== null && (highest === null || value > highest))
      highest = value;
  }
  return highest === null
    ? "No data in this window."
    : `Highest ${formatCount(highest)}${unit} in one bucket of the window.`;
}

function memoryFigure(
  input: FiguresInput,
  previous: SeriesSummary | null,
): Figure {
  const { summary, buckets, isPlatform, span } = input;
  const usesPlatformMemory = isPlatform && summary.memUsedMbMax !== null;
  const used = usesPlatformMemory
    ? (summary.memUsedMbMax ?? 0)
    : summary.rssMbMax;
  const before =
    previous === null
      ? null
      : usesPlatformMemory
        ? previous.memUsedMbMax
        : previous.rssMbMax;
  const limit = summary.memLimitMb;
  let detail = isPlatform
    ? "highest memory the platform used"
    : "highest resident memory of the app";
  if (usesPlatformMemory && limit !== null && limit > 0) {
    detail = `${Math.round((used / limit) * PERCENT)}% of the ${formatCount(limit)} MB limit`;
  }
  const spark = sparkOf(buckets, (bucket) =>
    usesPlatformMemory ? bucket.memUsedMbMax : bucket.rssMbMax,
  );
  return {
    key: "memory",
    label: "Memory peak",
    value: formatCount(used),
    unit: "MB",
    detail,
    spark,
    sparkText: highestText(spark, " MB"),
    change: countChange(used, before, span, { more: "higher", less: "lower" }),
  };
}

function latencyFigure(
  input: FiguresInput,
  previous: SeriesSummary | null,
  percent: number,
): Figure {
  const { summary, buckets, span } = input;
  const bucket = percentileBucket(summary.latHist, percent);
  const { value, unit } = latencyValue(bucket);
  const isMedian = percent === MEDIAN_PERCENT;
  const spark = sparkOf(buckets, (item) =>
    percentileBucket(item.latHist, percent),
  );
  return {
    key: isMedian ? "p50" : "p95",
    label: isMedian ? "Response time, p50" : "Response time, p95",
    value,
    unit,
    detail:
      bucket === null
        ? "no requests"
        : isMedian
          ? "half of the requests took at most this"
          : "95 in 100 requests took at most this",
    spark,
    sparkText: "Latency bucket of each part of the window, higher is slower.",
    change: latencyChange(
      bucket,
      previous === null ? null : percentileBucket(previous.latHist, percent),
      span,
    ),
  };
}

/**
 * The figures of one series: requests, server errors, the p50 and p95, the
 * memory peak, the egress drops and, for the platform, the apps running. A
 * previous window with no data gives no change: "up from none" would only say
 * the data starts here.
 */
export function buildFigures(input: FiguresInput): Figure[] {
  const { summary, buckets, isPlatform, span } = input;
  const previous =
    input.previous !== null && input.previous.reportedMinutes > 0
      ? input.previous
      : null;
  const requestsSpark = sparkOf(buckets, (bucket) => bucket.requests);
  const errorsSpark = sparkOf(buckets, (bucket) => bucket.r5xx);
  const egressSpark = sparkOf(buckets, (bucket) => bucket.egressDropped);

  const figures: Figure[] = [
    {
      key: "requests",
      label: "Requests",
      value: formatCount(summary.requests),
      unit: null,
      detail:
        summary.requests > 0
          ? `${shareText(summary.r4xx, summary.requests)} answered 4xx`
          : "no requests in this window",
      spark: requestsSpark,
      sparkText: highestText(requestsSpark, " requests"),
      change: countChange(summary.requests, previous?.requests ?? null, span),
    },
    {
      key: "errors",
      label: "Server errors",
      value: formatCount(summary.r5xx),
      unit: null,
      detail:
        summary.requests > 0
          ? `${shareText(summary.r5xx, summary.requests)} of requests`
          : "no requests",
      spark: errorsSpark,
      sparkText: highestText(errorsSpark, " server errors"),
      change: countChange(summary.r5xx, previous?.r5xx ?? null, span),
    },
    latencyFigure(input, previous, MEDIAN_PERCENT),
    latencyFigure(input, previous, TAIL_PERCENT),
    memoryFigure(input, previous),
    {
      key: "egress",
      label: "Egress drops",
      value: formatCount(summary.egressDropped),
      unit: null,
      detail:
        summary.egressDropped > 0
          ? "packets dropped by egress rules"
          : "nothing was blocked",
      spark: egressSpark,
      sparkText: highestText(egressSpark, " drops"),
      change: countChange(
        summary.egressDropped,
        previous?.egressDropped ?? null,
        span,
      ),
    },
  ];

  if (isPlatform && summary.childrenRunning !== null) {
    const appsSpark = sparkOf(buckets, (bucket) => bucket.childrenRunning);
    figures.push({
      key: "apps",
      label: "Apps running",
      value: formatCount(summary.childrenRunning),
      unit:
        summary.childrenRegistered === null
          ? null
          : `of ${formatCount(summary.childrenRegistered)}`,
      detail: "registered apps running at the end of the window",
      spark: appsSpark,
      sparkText: highestText(appsSpark, " apps running"),
      change: null,
    });
  }
  return figures;
}
