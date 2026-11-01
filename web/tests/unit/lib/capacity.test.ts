import assert from "node:assert/strict";
import { describe, it } from "vitest";

import {
  CAPACITY_CELL_COUNT,
  CAPACITY_DANGER_RATIO,
  CAPACITY_WARNING_RATIO,
  allocateCells,
  capacityCells,
  capacityLevel,
  levelFill,
} from "@/lib/capacity";

const sum = (values: readonly number[]) =>
  values.reduce((total, value) => total + value, 0);

describe("capacityLevel", () => {
  it("is normal below three quarters, warning from three quarters, danger from nine tenths", () => {
    assert.ok(CAPACITY_WARNING_RATIO < CAPACITY_DANGER_RATIO);
    assert.equal(capacityLevel(0, 1000), "normal");
    assert.equal(capacityLevel(749, 1000), "normal");
    assert.equal(capacityLevel(750, 1000), "warning");
    assert.equal(capacityLevel(899, 1000), "warning");
    assert.equal(capacityLevel(900, 1000), "danger");
    assert.equal(capacityLevel(5000, 1000), "danger");
  });

  it("stays normal when there is no scale to compare against", () => {
    assert.equal(capacityLevel(500, 0), "normal");
    assert.equal(capacityLevel(500, -1), "normal");
    assert.equal(capacityLevel(500, Number.NaN), "normal");
    assert.equal(capacityLevel(Number.NaN, 1000), "normal");
  });
});

describe("levelFill", () => {
  it("is neutral ink until a threshold, then the warning or danger fill", () => {
    assert.equal(levelFill("normal"), "used");
    assert.equal(levelFill("warning"), "warning");
    assert.equal(levelFill("danger"), "danger");
  });
});

describe("allocateCells", () => {
  it("gives each amount its share of the cells", () => {
    assert.deepEqual(allocateCells([5, 25], 100, 20), [1, 5]);
    assert.deepEqual(allocateCells([50], 100, 20), [10]);
  });

  it("rounds the whole fill, not each part, so the parts add up to it", () => {
    // 1024 + 5775 of 8192 MB is 83% of the strip: 16.6 cells, 17 drawn.
    const cells = allocateCells([1024, 5775], 8192, CAPACITY_CELL_COUNT);
    assert.equal(sum(cells), 17);
    assert.deepEqual(cells, [3, 14]);
  });

  it("draws at least one cell for any amount above zero, so a small use is not invisible", () => {
    assert.deepEqual(allocateCells([1], 10000, 20), [1]);
    assert.deepEqual(allocateCells([0], 10000, 20), [0]);
  });

  it("never draws more cells than the strip has, even when the parts exceed the total", () => {
    const over = allocateCells([1024, 7500], 8192, 20);
    assert.equal(sum(over), 20);
    const crowded = allocateCells([1, 1, 9998], 10000, 20);
    assert.ok(sum(crowded) <= 20);
    assert.ok(crowded.every((count) => count >= 1));
  });

  it("draws nothing when the scale is missing or the amounts are not numbers", () => {
    assert.deepEqual(allocateCells([5, 5], 0, 20), [0, 0]);
    assert.deepEqual(allocateCells([5, 5], Number.NaN, 20), [0, 0]);
    assert.deepEqual(allocateCells([-5, Number.NaN], 100, 20), [0, 0]);
  });
});

describe("capacityCells", () => {
  it("lists the segments in order and pads the rest of the strip as free", () => {
    const cells = capacityCells(
      [
        { amount: 5, fill: "reserved" },
        { amount: 50, fill: "used" },
      ],
      100,
      20,
    );
    assert.equal(cells.length, 20);
    assert.deepEqual(cells.slice(0, 1), ["reserved"]);
    assert.deepEqual(cells.slice(1, 11), Array(10).fill("used"));
    assert.deepEqual(cells.slice(11), Array(9).fill("free"));
  });

  it("does not draw a segment whose fill is free; free is whatever is left", () => {
    const cells = capacityCells(
      [
        { amount: 50, fill: "used" },
        { amount: 50, fill: "free" },
      ],
      100,
      20,
    );
    assert.equal(cells.filter((fill) => fill === "used").length, 10);
    assert.equal(cells.filter((fill) => fill === "free").length, 10);
  });

  it("is all free for an empty slot", () => {
    assert.deepEqual(capacityCells([{ amount: 0, fill: "used" }], 100, 4), [
      "free",
      "free",
      "free",
      "free",
    ]);
  });

  it("fills the whole strip in the danger fill when the slot is over-committed", () => {
    const cells = capacityCells([{ amount: 9000, fill: "danger" }], 8000, 20);
    assert.deepEqual(cells, Array(20).fill("danger"));
  });
});
