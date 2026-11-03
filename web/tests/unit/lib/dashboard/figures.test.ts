import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { MetricsRow } from "@/lib/api/schemas";
import {
  PREVIOUS_WINDOW_MIN_COVERAGE,
  buildFigures,
  countChange,
  latencyChange,
  latencyValue,
  sparklinePath,
} from "@/lib/dashboard/figures";
import type { PreviousWindow } from "@/lib/dashboard/figures";
import {
  MINUTE_MS,
  bucketRows,
  summarizeRows,
  windowDomain,
} from "@/lib/dashboard/series";
import { readExampleObject } from "../../../support/contract-examples";

const T0 = Date.parse("2026-10-30T09:00:00Z");

function isoAt(offsetMinutes: number): string {
  return new Date(T0 + offsetMinutes * MINUTE_MS)
    .toISOString()
    .replace(".000Z", "Z");
}

function row(
  offsetMinutes: number,
  overrides: Record<string, unknown> = {},
): MetricsRow {
  return MetricsRow.parse({
    ...readExampleObject("MetricsRow"),
    app_slug: "sales",
    minute: isoAt(offsetMinutes),
    ...overrides,
  });
}

describe("countChange", () => {
  it("says how much more or fewer than the previous window, in whole percent", () => {
    assert.deepEqual(countChange(112, 100, "hour"), {
      direction: "up",
      text: "12% more than the previous hour",
    });
    assert.deepEqual(countChange(50, 200, "6 hours"), {
      direction: "down",
      text: "75% fewer than the previous 6 hours",
    });
  });

  it("calls a change that rounds to 0% about the same, and two zeros the same", () => {
    assert.equal(countChange(1001, 1000, "hour")?.direction, "same");
    assert.equal(
      countChange(1001, 1000, "hour")?.text,
      "About the same as the previous hour",
    );
    assert.equal(countChange(0, 0, "hour")?.text, "Same as the previous hour");
  });

  it("says a rise from nothing in words, not as an infinite percent", () => {
    assert.deepEqual(countChange(5, 0, "24 hours"), {
      direction: "up",
      text: "Up from none in the previous 24 hours",
    });
  });

  it("uses the words it is given, for a peak rather than a count", () => {
    assert.equal(
      countChange(90, 100, "hour", { more: "higher", less: "lower" })?.text,
      "10% lower than the previous hour",
    );
  });

  it("is nothing when there is no previous window to compare with", () => {
    assert.equal(countChange(5, null, "hour"), null);
  });
});

describe("latencyChange", () => {
  it("says faster or slower, with the earlier bound, by latency bucket", () => {
    assert.deepEqual(latencyChange(4, 2, "hour"), {
      direction: "up",
      text: "Slower than the previous hour (at most 25 ms then)",
    });
    assert.deepEqual(latencyChange(2, 4, "hour"), {
      direction: "down",
      text: "Faster than the previous hour (at most 100 ms then)",
    });
    assert.equal(latencyChange(3, 3, "hour")?.direction, "same");
  });

  it("is nothing when either window had no request", () => {
    assert.equal(latencyChange(null, 3, "hour"), null);
    assert.equal(latencyChange(3, null, "hour"), null);
  });
});

describe("latencyValue", () => {
  it("gives a percentile's bucket as a number and a unit", () => {
    assert.deepEqual(latencyValue(2), { value: "≤ 25", unit: "ms" });
    assert.deepEqual(latencyValue(8), { value: "≤ 2.5", unit: "s" });
    assert.deepEqual(latencyValue(10), { value: "> 5", unit: "s" });
    assert.deepEqual(latencyValue(null), { value: "None", unit: null });
  });
});

describe("sparklinePath", () => {
  it("draws the values from left to right, the highest at the top", () => {
    assert.equal(sparklinePath([0, 5, 10], 100, 20), "M0 18L50 10L100 2");
  });

  it("breaks the line at a gap and keeps a lone point as a dot", () => {
    assert.equal(
      sparklinePath([10, null, 10, 0], 90, 20),
      "M0 2L0 2M60 2L90 18",
    );
  });

  it("draws a flat line along the bottom when every value is zero, and nothing without values", () => {
    assert.equal(sparklinePath([0, 0], 10, 20), "M0 18L10 18");
    assert.equal(sparklinePath([], 10, 20), "");
    assert.equal(sparklinePath([null, null], 10, 20), "");
  });

  it("puts a single value in the middle", () => {
    assert.equal(sparklinePath([3], 100, 20), "M50 2L50 2");
  });
});

describe("buildFigures", () => {
  const hour = windowDomain(isoAt(0), isoAt(60));
  assert.ok(hour !== null);
  const HOUR_MINUTES = 60;

  /** A minute the app ran through without a request: a row is still written. */
  function quiet(offsetMinutes: number): MetricsRow {
    return row(offsetMinutes, {
      requests: 0,
      r2xx: 0,
      r3xx: 0,
      r4xx: 0,
      r5xx: 0,
      upstream_errors: 0,
      egress_dropped: 0,
      lat_hist: Array.from({ length: 11 }, () => 0),
    });
  }

  /** The hour before, holding a row for each of its first `minutes` minutes. */
  function previousHour(
    minutes: number,
    overrides: Record<string, unknown> = {},
  ): PreviousWindow {
    const rows = Array.from({ length: minutes }, (_, offset) =>
      row(offset - HOUR_MINUTES, overrides),
    );
    return { summary: summarizeRows(rows), windowMinutes: HOUR_MINUTES };
  }

  it("gives every figure a number, a label, a sparkline per bucket and the change on the previous window", () => {
    const rows = [
      row(0, { requests: 100, r5xx: 2, rss_mb_max: 300, egress_dropped: 1 }),
      row(1, { requests: 50, r5xx: 0, rss_mb_max: 340, egress_dropped: 0 }),
    ];
    // The hour before ran all its minutes; only its first one had requests.
    const previous: PreviousWindow = {
      summary: summarizeRows([
        row(-60, { requests: 300, r5xx: 1 }),
        ...Array.from({ length: 59 }, (_, offset) => quiet(offset - 59)),
      ]),
      windowMinutes: HOUR_MINUTES,
    };
    const { buckets } = bucketRows(rows, hour);
    const figures = buildFigures({
      summary: summarizeRows(rows),
      previous,
      buckets,
      isPlatform: false,
      span: "hour",
    });
    assert.deepEqual(
      figures.map((figure) => figure.key),
      ["requests", "errors", "p50", "p95", "memory", "egress"],
    );
    const requests = figures[0];
    assert.equal(requests?.value, "150");
    assert.equal(requests?.label, "Requests");
    assert.equal(requests?.spark.length, 60);
    assert.deepEqual(requests?.spark.slice(0, 3), [100, 50, null]);
    assert.equal(requests?.change?.text, "50% fewer than the previous hour");
    const errors = figures[1];
    assert.equal(errors?.value, "2");
    assert.equal(errors?.detail, "1.3% of requests");
    const memory = figures[4];
    assert.equal(memory?.value, "340");
    assert.equal(memory?.unit, "MB");
  });

  it("shows no change when the previous window had no data or was not read", () => {
    const rows = [row(0, { requests: 100 })];
    const { buckets } = bucketRows(rows, hour);
    for (const previous of [null, previousHour(0)]) {
      const figures = buildFigures({
        summary: summarizeRows(rows),
        previous,
        buckets,
        isPlatform: false,
        span: "hour",
      });
      assert.ok(figures.every((figure) => figure.change === null));
    }
  });

  describe("a previous window that holds only part of its minutes", () => {
    // Flat traffic: 10 requests every minute of both hours.
    const rows = Array.from({ length: HOUR_MINUTES }, (_, offset) =>
      row(offset, { requests: 10 }),
    );
    const { buckets } = bucketRows(rows, hour);
    const figuresAgainst = (previous: PreviousWindow) =>
      buildFigures({
        summary: summarizeRows(rows),
        previous,
        buckets,
        isPlatform: false,
        span: "hour",
      });

    it("shows no change when it holds 10 of its 60 minutes (installed 70 minutes ago)", () => {
      const figures = figuresAgainst(previousHour(10, { requests: 10 }));
      assert.ok(figures.every((figure) => figure.change === null));
    });

    it("compares a whole window", () => {
      const figures = figuresAgainst(previousHour(60, { requests: 10 }));
      assert.equal(figures[0]?.change?.text, "Same as the previous hour");
    });

    it("compares from the required share of its minutes, and not one minute below it", () => {
      const enough = Math.ceil(HOUR_MINUTES * PREVIOUS_WINDOW_MIN_COVERAGE);
      assert.ok(
        figuresAgainst(previousHour(enough, { requests: 10 }))[0]?.change !==
          null,
      );
      assert.ok(
        figuresAgainst(previousHour(enough - 1, { requests: 10 }))[0]
          ?.change === null,
      );
    });
  });

  it("reads the platform's memory against its limit and adds the apps running", () => {
    const platform = (offset: number, used: number, running: number) =>
      row(offset, {
        app_slug: "_platform",
        mem_used_mb: used,
        mem_limit_mb: 6144,
        children_running: running,
        children_registered: 4,
      });
    const rows = [platform(0, 1500, 2), platform(1, 3072, 3)];
    const { buckets } = bucketRows(rows, hour);
    const figures = buildFigures({
      summary: summarizeRows(rows),
      previous: null,
      buckets,
      isPlatform: true,
      span: "hour",
    });
    const memory = figures.find((figure) => figure.key === "memory");
    assert.equal(memory?.value, "3,072");
    assert.equal(memory?.detail, "50% of the 6,144 MB limit");
    const apps = figures.find((figure) => figure.key === "apps");
    assert.equal(apps?.value, "3");
    assert.equal(apps?.unit, "of 4");
    assert.deepEqual(apps?.spark.slice(0, 2), [2, 3]);
  });
});
