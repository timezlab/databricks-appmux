import assert from "node:assert/strict";
import { describe, it } from "vitest";

import {
  HIGHLIGHT_KEY,
  LINE_LIMIT_KEY,
  LINE_VALUE_KEY,
  columnAxis,
  columnRows,
  gapRuns,
  lineAxis,
  lineRows,
  segmentKey,
} from "@/lib/dashboard/chart-data";
import type {
  ChartTone,
  ColumnChartSpec,
  LineChartSpec,
} from "@/lib/dashboard/model";

const TONES: ChartTone[] = ["primary", "warning", "danger"];

function columnSpec(
  columns: (number[] | null)[],
  overrides: Partial<ColumnChartSpec> = {},
): ColumnChartSpec {
  return {
    kind: "columns",
    id: "requests",
    title: "Requests per minute",
    description: "",
    legend: [],
    labels: columns.map((_unused, index) => `t${index}`),
    tips: columns.map((_unused, index) => `tip ${index}`),
    maxTicks: 6,
    xCaption: "Time (UTC)",
    table: { headers: [], rows: [] },
    emptyMessage: null,
    tones: TONES,
    columns,
    highlights: [],
    ...overrides,
  };
}

function lineSpec(
  values: (number | null)[],
  reference: number | null = null,
): LineChartSpec {
  return {
    kind: "line",
    id: "memory",
    title: "Memory peak per minute",
    description: "",
    legend: [],
    labels: values.map((_unused, index) => `t${index}`),
    tips: values.map((_unused, index) => `tip ${index}`),
    maxTicks: 6,
    xCaption: "Time (UTC)",
    table: { headers: [], rows: [] },
    emptyMessage: null,
    tone: "primary",
    values,
    reference,
    referenceLabel: reference === null ? null : "Memory limit",
    referenceTone: "danger",
  };
}

describe("columnRows", () => {
  it("gives one row per column, keyed by its position, with the tooltip text of that column", () => {
    const rows = columnRows(
      columnSpec([
        [3, 1, 0],
        [5, 0, 0],
      ]),
    );
    assert.equal(rows.length, 2);
    assert.equal(rows[0]?.index, 0);
    assert.equal(rows[1]?.index, 1);
    assert.equal(rows[1]?.tip, "tip 1");
  });

  it("keeps a segment's value under the key of its position, bottom first", () => {
    const [row] = columnRows(columnSpec([[3, 2, 1]]));
    assert.equal(row?.[segmentKey(0)], 3);
    assert.equal(row?.[segmentKey(1)], 2);
    assert.equal(row?.[segmentKey(2)], 1);
    assert.deepEqual(
      [segmentKey(0), segmentKey(1), segmentKey(2)].length,
      new Set([segmentKey(0), segmentKey(1), segmentKey(2)]).size,
    );
  });

  it("makes a segment of zero null, so it has no height to draw and no outline", () => {
    const [row] = columnRows(columnSpec([[0, 5, 0]]));
    assert.equal(row?.[segmentKey(0)], null);
    assert.equal(row?.[segmentKey(1)], 5);
    assert.equal(row?.[segmentKey(2)], null);
  });

  it("makes every segment of a minute with no data null, unlike a minute of zeros that is a row too", () => {
    const rows = columnRows(columnSpec([null, [0, 0, 0]]));
    for (const row of rows) {
      for (const segment of TONES.keys()) {
        assert.equal(row[segmentKey(segment)], null);
      }
    }
    assert.equal(rows.length, 2);
  });

  it("treats a column with fewer values than segments as having none for the rest", () => {
    const [row] = columnRows(columnSpec([[7]]));
    assert.equal(row?.[segmentKey(0)], 7);
    assert.equal(row?.[segmentKey(1)], null);
  });

  it("carries the label of a highlighted column and nothing for the others", () => {
    const rows = columnRows(
      columnSpec([[1], [9], [2]], {
        tones: ["primary"],
        highlights: [{ index: 1, label: "p50" }],
      }),
    );
    assert.deepEqual(
      rows.map((row) => row[HIGHLIGHT_KEY]),
      [null, "p50", null],
    );
  });

  it("gives no rows for no columns", () => {
    assert.deepEqual(columnRows(columnSpec([])), []);
  });
});

describe("columnAxis", () => {
  it("starts at zero and reaches the tallest stack, not the tallest segment", () => {
    const axis = columnAxis(
      columnSpec([
        [100, 50, 20],
        [30, 0, 0],
      ]),
    );
    assert.equal(axis.ticks[0], 0);
    assert.ok(axis.max >= 170, `max ${axis.max}`);
  });

  it("gives an axis from 0 to 1 when there is nothing to draw", () => {
    assert.deepEqual(columnAxis(columnSpec([null, null])).ticks, [0, 1]);
    assert.deepEqual(columnAxis(columnSpec([])).ticks, [0, 1]);
  });

  it("ignores a negative value instead of letting it shrink the axis", () => {
    const axis = columnAxis(columnSpec([[-5, 10]]));
    assert.ok(axis.max >= 10);
  });
});

describe("gapRuns", () => {
  it("shades the stretches of no data that lie between two minutes with data", () => {
    assert.deepEqual(gapRuns([[1], null, null, [2], null, [3]]), [
      { first: 1, last: 2 },
      { first: 4, last: 4 },
    ]);
  });

  it("shades nothing before the first minute with data (the app was not there yet) or after the last (not reported yet)", () => {
    assert.deepEqual(gapRuns([null, null, [1], [2], null, null]), []);
  });

  it("does not shade a minute of zeros: it is a row, not a gap", () => {
    assert.deepEqual(gapRuns([[1], [0], [2]]), []);
  });

  it("shades nothing for no data at all, or a single minute", () => {
    assert.deepEqual(gapRuns([]), []);
    assert.deepEqual(gapRuns([null, null, null]), []);
    assert.deepEqual(gapRuns([null, [4], null]), []);
  });
});

describe("lineRows", () => {
  it("keeps a minute with no data null, never zero, so the line breaks there", () => {
    const rows = lineRows(lineSpec([5, null, 7]));
    assert.deepEqual(
      rows.map((row) => row[LINE_VALUE_KEY]),
      [5, null, 7],
    );
  });

  it("repeats the limit on every row, so it is a level line, even where the value has no data", () => {
    const rows = lineRows(lineSpec([5, null, 7], 100));
    assert.deepEqual(
      rows.map((row) => row[LINE_LIMIT_KEY]),
      [100, 100, 100],
    );
  });

  it("has no limit on any row when there is none, or when it is zero or below", () => {
    for (const reference of [null, 0, -3]) {
      const rows = lineRows(lineSpec([5, 6], reference));
      assert.deepEqual(
        rows.map((row) => row[LINE_LIMIT_KEY]),
        [null, null],
      );
    }
  });

  it("carries the tooltip text and the position of each column", () => {
    const rows = lineRows(lineSpec([5, 6]));
    assert.deepEqual(
      rows.map((row) => [row.index, row.tip]),
      [
        [0, "tip 0"],
        [1, "tip 1"],
      ],
    );
  });
});

describe("lineAxis", () => {
  it("starts at zero, so a line never exaggerates a change", () => {
    const axis = lineAxis(lineSpec([5000, 5100, 5200]));
    assert.equal(axis.ticks[0], 0);
  });

  it("keeps the limit on the chart even when the data is far below it", () => {
    const axis = lineAxis(lineSpec([10, 20], 7168));
    assert.ok(axis.max >= 7168, `max ${axis.max}`);
  });

  it("reaches the data when it is above the limit", () => {
    const axis = lineAxis(lineSpec([9000], 7168));
    assert.ok(axis.max >= 9000, `max ${axis.max}`);
  });

  it("ignores minutes with no data and gives 0 to 1 for a line with none", () => {
    assert.deepEqual(lineAxis(lineSpec([null, null])).ticks, [0, 1]);
  });
});
