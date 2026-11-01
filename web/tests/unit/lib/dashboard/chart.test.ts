import assert from "node:assert/strict";
import { describe, it } from "vitest";

import {
  clockLabel,
  formatAxisNumber,
  markerIndices,
  niceScale,
  xTickIndices,
} from "@/lib/dashboard/chart";

describe("niceScale", () => {
  it("rounds the top of the axis up to 1, 2 or 5 times a power of ten", () => {
    assert.deepEqual(niceScale(412), {
      max: 600,
      step: 200,
      ticks: [0, 200, 400, 600],
    });
    assert.deepEqual(niceScale(7), { max: 8, step: 2, ticks: [0, 2, 4, 6, 8] });
    assert.deepEqual(niceScale(20), {
      max: 20,
      step: 5,
      ticks: [0, 5, 10, 15, 20],
    });
    assert.deepEqual(niceScale(10_000), {
      max: 10_000,
      step: 5000,
      ticks: [0, 5000, 10_000],
    });
  });

  it("steps by whole numbers, because every figure is a count or a size in whole units", () => {
    assert.deepEqual(niceScale(1), { max: 1, step: 1, ticks: [0, 1] });
    assert.deepEqual(niceScale(3), { max: 3, step: 1, ticks: [0, 1, 2, 3] });
  });

  it("gives an empty chart a one-step axis instead of dividing by zero", () => {
    for (const value of [0, -4, Number.NaN, Number.POSITIVE_INFINITY]) {
      assert.deepEqual(
        niceScale(value),
        { max: 1, step: 1, ticks: [0, 1] },
        String(value),
      );
    }
  });

  it("never ends below the largest value, from 1 to billions", () => {
    for (const value of [
      1, 2, 9, 11, 99, 101, 412, 999, 1001, 123_456, 2_000_000_000, 3.5e12,
    ]) {
      const { max, step, ticks } = niceScale(value);
      assert.ok(max >= value, `${value} -> ${max}`);
      assert.ok(
        ticks.length >= 2 && ticks.length <= 6,
        `${value}: ${ticks.length} ticks`,
      );
      assert.equal(ticks[ticks.length - 1], max);
      assert.ok(Number.isInteger(step));
    }
  });
});

describe("formatAxisNumber", () => {
  it("prints in full under 10,000 and compactly from there, one style per axis", () => {
    assert.equal(formatAxisNumber(2000, 8000), "2,000");
    assert.equal(formatAxisNumber(0, 8000), "0");
    assert.equal(formatAxisNumber(0, 20_000), "0");
    assert.equal(formatAxisNumber(5000, 20_000), "5K");
    assert.equal(formatAxisNumber(12_500, 50_000), "12.5K");
    assert.equal(formatAxisNumber(1_500_000, 2_000_000), "1.5M");
    assert.equal(formatAxisNumber(2_000_000_000, 2_000_000_000), "2B");
  });
});

describe("xTickIndices", () => {
  it("labels about six columns of an hour, every column of a short chart, and the first of one", () => {
    assert.deepEqual(xTickIndices(60, 6), [0, 10, 20, 30, 40, 50]);
    assert.deepEqual(xTickIndices(72, 6), [0, 12, 24, 36, 48, 60]);
    assert.deepEqual(xTickIndices(11, 11), [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
    assert.deepEqual(xTickIndices(1, 6), [0]);
    assert.deepEqual(xTickIndices(0, 6), []);
  });

  it("never returns more labels than asked for, for any number of columns", () => {
    for (let count = 1; count <= 144; count += 1) {
      const indices = xTickIndices(count, 6);
      assert.ok(indices.length <= 6, `${count}: ${indices.length}`);
      assert.ok(indices.every((index) => index < count));
      assert.equal(indices[0], 0);
    }
  });
});

describe("clockLabel", () => {
  const ms = Date.parse("2026-10-30T09:05:42Z");
  it("is the UTC time, with the date when the window spans days", () => {
    assert.equal(clockLabel(ms, false), "09:05");
    assert.equal(clockLabel(ms, true), "10-30 09:05");
  });
});

describe("markerIndices", () => {
  it("marks the newest point of a line, and only that one when the line is joined up", () => {
    assert.deepEqual(markerIndices([4, 5, 6, 7]), [3]);
  });

  it("marks the newest point that has data when the last minutes have none", () => {
    assert.deepEqual(markerIndices([4, 5, null, null]), [1]);
  });

  it("marks a point with no neighbour, because a line of one point has no length to show it by", () => {
    assert.deepEqual(markerIndices([null, 3, null, 4, 5, null]), [1, 4]);
    assert.deepEqual(markerIndices([9]), [0]);
  });

  it("marks the end of a stretch of one point at the edges of the window too", () => {
    assert.deepEqual(markerIndices([3, null, 4, 5]), [0, 3]);
  });

  it("marks nothing for no data, and never a minute with no data", () => {
    assert.deepEqual(markerIndices([]), []);
    assert.deepEqual(markerIndices([null, null, null]), []);
  });

  it("treats a zero as data: a measured zero is a point, not a gap", () => {
    assert.deepEqual(markerIndices([0, 0, 0]), [2]);
  });
});
