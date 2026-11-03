/**
 * A chart spec (see `model.ts`) turned into what Recharts takes: one row per
 * column with a numeric key per series, the stretches to shade as "no data",
 * and the axis. The rules here are the ones a reader relies on and a picture
 * cannot show by itself: a minute with no data is `null`, never zero (a line
 * breaks, a bar is absent), a zero segment is not drawn, and an axis starts at
 * zero and reaches the data and the limit.
 */
import { niceScale } from "./chart";
import type { NiceScale } from "./chart";
import type { ChartSpec, ColumnChartSpec, LineChartSpec } from "./model";

/** Recharts reads a series from a row by key. */
export interface ChartRow {
  /**
   * The column's position. The category axis is keyed by it (the label is the
   * tick text), so two columns with the same label never merge into one.
   */
  index: number;
  /** What the tooltip says for this column. */
  tip: string;
  [seriesKey: string]: number | string | null;
}

/** The row key of the stacked segment at `segment` in `ColumnChartSpec.tones`. */
export function segmentKey(segment: number): string {
  return `segment${segment}`;
}

export const HIGHLIGHT_KEY = "highlight";
export const LINE_VALUE_KEY = "value";
export const LINE_LIMIT_KEY = "limit";

function positiveSum(values: readonly number[] | null): number {
  return (values ?? []).reduce(
    (sum, value) => sum + (value > 0 ? value : 0),
    0,
  );
}

/**
 * One row per column. A segment of zero is `null` (it has no height to draw,
 * and a drawn outline of height zero would show as a stray line); a column
 * with no data has every segment `null`.
 */
export function columnRows(spec: ColumnChartSpec): ChartRow[] {
  const highlightLabel = new Map(
    spec.highlights.map((highlight) => [highlight.index, highlight.label]),
  );
  return spec.columns.map((values, index) => {
    const row: ChartRow = {
      index,
      tip: spec.tips[index] ?? "",
      [HIGHLIGHT_KEY]: highlightLabel.get(index) ?? null,
    };
    for (const segment of spec.tones.keys()) {
      const value = values?.[segment];
      row[segmentKey(segment)] =
        value !== undefined && value > 0 ? value : null;
    }
    return row;
  });
}

/** The axis of a column chart: from zero to a round number at or above the tallest stack. */
export function columnAxis(spec: ColumnChartSpec): NiceScale {
  return niceScale(Math.max(0, ...spec.columns.map(positiveSum)));
}

export interface GapRun {
  /** Index of the first and the last column of the stretch. */
  first: number;
  last: number;
}

/**
 * The stretches of columns with no data that lie between two columns with
 * data, to shade. Before the first and after the last column with data there
 * is no stretch: the app was not there yet, or the window just ended.
 */
export function gapRuns(
  columns: readonly (readonly number[] | null)[],
): GapRun[] {
  const firstData = columns.findIndex((values) => values !== null);
  const lastData = columns.findLastIndex((values) => values !== null);
  const runs: GapRun[] = [];
  let runStart: number | null = null;
  for (let index = firstData + 1; index <= lastData; index++) {
    if (columns[index] === null) {
      runStart ??= index;
    } else if (runStart !== null) {
      runs.push({ first: runStart, last: index - 1 });
      runStart = null;
    }
  }
  return runs;
}

/** The limit as a level, or `null` when there is none (zero and below are none). */
function limitOf(spec: LineChartSpec): number | null {
  return spec.reference !== null && spec.reference > 0 ? spec.reference : null;
}

/** One row per column: the value (`null` for no data, which breaks the line) and the limit, which stays level. */
export function lineRows(spec: LineChartSpec): ChartRow[] {
  const limit = limitOf(spec);
  return spec.values.map((value, index) => ({
    index,
    tip: spec.tips[index] ?? "",
    [LINE_VALUE_KEY]: value,
    [LINE_LIMIT_KEY]: limit,
  }));
}

/** The axis of a line chart: from zero, reaching the limit even when the data is far below it. */
export function lineAxis(spec: LineChartSpec): NiceScale {
  const data = spec.values.filter((value): value is number => value !== null);
  return niceScale(Math.max(0, limitOf(spec) ?? 0, ...data));
}

/**
 * The `fill-opacity` of a mark whose bucket the window cuts short. It holds
 * fewer minutes than the others, so its smaller total must not read as a dip:
 * the mark is fainter, the caption under the axis says why, and its tip and
 * table row say "(partial)". Half strength keeps the mark plainly there.
 */
export const PARTIAL_MARK_OPACITY = 0.5;

/**
 * The `fill-opacity` attribute of the mark at `index`: fainter for a partial
 * bucket, and none at all (full strength) for a whole one. A presentation
 * attribute, not a style, so the strict CSP allows it.
 */
export function markOpacity(
  partial: readonly boolean[],
  index: number,
): number | undefined {
  return partial[index] === true ? PARTIAL_MARK_OPACITY : undefined;
}

/** The text under the axis: the spec's caption, and what a fainter mark is when the chart has one. */
export function axisCaption(spec: ChartSpec): string {
  if (!spec.partial.includes(true)) return spec.xCaption;
  const mark = spec.kind === "columns" ? "column" : "point";
  return `${spec.xCaption}. A fainter ${mark} is a partial bucket: the window holds only part of it`;
}
