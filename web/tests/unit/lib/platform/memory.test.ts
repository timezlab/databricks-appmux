import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { memoryView, totalBudgetMb } from "@/lib/platform/memory";

// The worked example of design 5.7 and 10.1: 8192 MB limit, 1024 MB reserve, 5775 MB committed.
const EXAMPLE = {
  limit_mb: 8192,
  reserve_mb: 1024,
  committed_mb: 5775,
  free_mb: 1393,
};

describe("memoryView", () => {
  it("reads the design's example: 5,775 of 7,168 MB, 81%", () => {
    const view = memoryView(EXAMPLE);
    assert.equal(view.availableMb, 7168);
    assert.equal(view.percentText, "81%");
    assert.equal(
      view.headline,
      "5,775 of 7,168 MB committed (81%); 1,393 MB free",
    );
    assert.equal(view.note, null);
  });

  it("shows the server's figures as they are, not recomputed ones", () => {
    const view = memoryView({ ...EXAMPLE, free_mb: 1400 });
    assert.equal(view.freeMb, 1400);
  });

  it("is calm while memory is plentiful, then warns, then raises the alarm", () => {
    const at = (committed: number) =>
      memoryView({
        limit_mb: 1100,
        reserve_mb: 100,
        committed_mb: committed,
        free_mb: 1000 - committed,
      }).level;
    assert.equal(at(0), "normal");
    assert.equal(at(749), "normal");
    assert.equal(at(750), "warning");
    assert.equal(at(899), "warning");
    assert.equal(at(900), "danger");
    assert.equal(at(1000), "danger");
  });

  it("names an over-commitment instead of printing a negative free figure", () => {
    const view = memoryView({
      limit_mb: 8192,
      reserve_mb: 1024,
      committed_mb: 7500,
      free_mb: -332,
    });
    assert.equal(view.level, "danger");
    assert.equal(view.levelText, "Over-committed");
    assert.equal(view.freeMb, -332);
    assert.match(view.note ?? "", /over-committed by 332 MB/i);
    assert.doesNotMatch(view.headline, /-/);
  });

  it("does not divide by zero when the reserve takes the whole limit", () => {
    const view = memoryView({
      limit_mb: 1024,
      reserve_mb: 1024,
      committed_mb: 0,
      free_mb: 0,
    });
    assert.equal(view.level, "danger");
    assert.equal(view.levelText, "No memory available");
    assert.equal(view.percentText, "unknown");
    assert.match(view.note ?? "", /no memory is available/i);
  });
});

describe("memoryView strip", () => {
  it("splits the container limit into reserved, committed and free, in that order", () => {
    const view = memoryView(EXAMPLE);
    assert.deepEqual(
      view.segments.map((segment) => [segment.key, segment.valueText]),
      [
        ["reserved", "1,024 MB"],
        ["committed", "5,775 MB"],
        ["free", "1,393 MB"],
      ],
    );
    assert.deepEqual(
      view.segments.map((segment) => segment.fill),
      // 81% of the apps' share is past the 75% warning threshold.
      ["reserved", "warning", "free"],
    );
  });

  it("paints the committed share in the warning and danger fills only past the thresholds", () => {
    const committedFill = (committed: number) =>
      memoryView({
        limit_mb: 1100,
        reserve_mb: 100,
        committed_mb: committed,
        free_mb: 1000 - committed,
      }).segments[1]?.fill;
    assert.equal(committedFill(749), "used");
    assert.equal(committedFill(750), "warning");
    assert.equal(committedFill(900), "danger");
  });

  it("names the level in words for a reader who cannot tell the fills apart", () => {
    const text = (committed: number) =>
      memoryView({
        limit_mb: 1100,
        reserve_mb: 100,
        committed_mb: committed,
        free_mb: 1000 - committed,
      }).levelText;
    assert.equal(text(100), null);
    assert.equal(text(800), "Getting full");
    assert.equal(text(950), "Almost full");
  });

  it("states every figure in the accessible summary and prints no negative free memory", () => {
    assert.equal(
      memoryView(EXAMPLE).summary,
      "Memory: 1,024 MB reserved for appmux, 5,775 MB committed to apps, 1,393 MB free, of the 8,192 MB the container allows.",
    );
    const over = memoryView({
      limit_mb: 8192,
      reserve_mb: 1024,
      committed_mb: 7500,
      free_mb: -332,
    });
    assert.match(over.summary, /0 MB free/);
    assert.doesNotMatch(over.summary, /-/);
  });
});

describe("totalBudgetMb", () => {
  it("adds the budgets of every registered app, running or not", () => {
    assert.equal(
      totalBudgetMb([
        { memory_budget_mb: 512 },
        { memory_budget_mb: 256 },
        { memory_budget_mb: 256 },
      ]),
      1024,
    );
    assert.equal(totalBudgetMb([]), 0);
  });
});
