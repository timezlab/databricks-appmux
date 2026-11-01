/**
 * The numbers behind the dashboard's chart axes, as plain functions: a round
 * axis top and its ticks, how an axis number is written, which columns get a
 * time label, how a time is written, and which points of a line get a marker.
 * Recharts draws the marks; these decide what they say, so the rules (the axis
 * starts at zero and never ends below the data, whole-number steps, a label
 * every few columns) are tested without a browser.
 */
import { formatCount } from "@/lib/format";

// ----------------------------------------------------------------- scale

const INTERVALS_WANTED = 4;
const NICE_MANTISSAS = [1, 2, 5, 10] as const;

export interface NiceScale {
  /** The top of the axis: a multiple of `step`, never below the data. */
  max: number;
  step: number;
  /** `0`, `step`, ... `max`. */
  ticks: number[];
}

/**
 * An axis from 0 up to a round number at or above `maxValue`, with a step of 1,
 * 2 or 5 times a power of ten and about four intervals. Steps are whole numbers
 * because every figure charted is a count, a size in MB or a number of
 * milliseconds. No data (zero, negative, not a number) gives the axis 0 to 1.
 */
export function niceScale(
  maxValue: number,
  intervals: number = INTERVALS_WANTED,
): NiceScale {
  const top = Number.isFinite(maxValue) && maxValue > 0 ? maxValue : 0;
  const rawStep = top / intervals;
  let step = 1;
  if (rawStep > 1) {
    // Found by multiplying, not by log10, so 1000 is never 2.9999999999999996.
    let magnitude = 1;
    while (magnitude * 10 <= rawStep) magnitude *= 10;
    const mantissa = rawStep / magnitude;
    step = magnitude * (NICE_MANTISSAS.find((nice) => mantissa <= nice) ?? 10);
  }
  const max = Math.max(Math.ceil(top / step), 1) * step;
  const ticks: number[] = [];
  for (let value = 0; value <= max; value += step) ticks.push(value);
  return { max, step, ticks };
}

const COMPACT_FROM = 10_000;
const COMPACT_UNITS = [
  { from: 1e9, suffix: "B" },
  { from: 1e6, suffix: "M" },
  { from: 1e3, suffix: "K" },
] as const;

/**
 * An axis label: in full under 10,000 (`2,000`), compact on an axis that
 * reaches it (`20K`, `1.5M`). One style per axis, chosen by the axis's top.
 */
export function formatAxisNumber(value: number, axisMax: number): string {
  if (axisMax < COMPACT_FROM) return formatCount(value);
  for (const unit of COMPACT_UNITS) {
    if (Math.abs(value) >= unit.from) {
      return `${Number((value / unit.from).toFixed(1))}${unit.suffix}`;
    }
  }
  return formatCount(value);
}

/** Label positions that look round when counted in columns: 1, 2, 3, ... 60, 72. */
const TICK_STEPS = [1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 24, 30, 36, 60, 72];

/** Which columns get a time label: the first, then every few, at most `maxTicks` in all. */
export function xTickIndices(count: number, maxTicks: number): number[] {
  if (count <= 0 || maxTicks < 1) return [];
  const step =
    TICK_STEPS.find((candidate) => Math.ceil(count / candidate) <= maxTicks) ??
    Math.ceil(count / maxTicks);
  const indices: number[] = [];
  for (let index = 0; index < count; index += step) indices.push(index);
  return indices;
}

function twoDigits(value: number): string {
  return String(value).padStart(2, "0");
}

/** `09:05` (UTC), or `10-30 09:05` when the window spans more than one day. */
export function clockLabel(ms: number, withDate: boolean): string {
  const date = new Date(ms);
  const clock = `${twoDigits(date.getUTCHours())}:${twoDigits(date.getUTCMinutes())}`;
  return withDate
    ? `${twoDigits(date.getUTCMonth() + 1)}-${twoDigits(date.getUTCDate())} ${clock}`
    : clock;
}

/**
 * The points of a line that get a marker: the newest point, and every point
 * with no neighbour (a line of one point has no length to show it by). A minute
 * with no data (`null`) breaks the line and is never a marker.
 */
export function markerIndices(values: readonly (number | null)[]): number[] {
  const newest = values.findLastIndex((value) => value !== null);
  return values.flatMap((value, index) => {
    if (value === null) return [];
    const isAlone =
      (values[index - 1] ?? null) === null &&
      (values[index + 1] ?? null) === null;
    return isAlone || index === newest ? [index] : [];
  });
}
