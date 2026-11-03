import assert from "node:assert/strict";
import { describe, it } from "vitest";

import type { AppSummary } from "@/lib/api/schemas";
import {
  BREAKDOWN_LIMIT,
  DEFAULT_RANGE_ID,
  MAX_WINDOW_MINUTES,
  MAX_WINDOW_STEPS,
  PLATFORM_APP_SLUG,
  TIME_RANGES,
  breakdownQueryFor,
  isTimeRangeId,
  longerRange,
  managedApps,
  metricsPollIntervalMs,
  metricsQueryFor,
  metricsWindow,
  previousWindow,
  stepForRange,
  stepMinutes,
  timeRangeById,
} from "@/lib/dashboard/scope";
import { MIN_POLL_INTERVAL_MS } from "@/lib/poll/schedule";

const DAY_MINUTES = 1440;

function app(slug: string, permission: AppSummary["permission"]): AppSummary {
  return {
    slug,
    display_name: `App ${slug}`,
    state: "running",
    permission,
    open_modes: ["as_user"],
  };
}

describe("time ranges", () => {
  it("run from 15 minutes to the 7 days the data is kept, shortest first, with unique ids and the default among them", () => {
    assert.deepEqual(
      TIME_RANGES.map((range) => range.minutes),
      [15, 60, 360, DAY_MINUTES, 3 * DAY_MINUTES, 7 * DAY_MINUTES],
    );
    assert.equal(new Set(TIME_RANGES.map((range) => range.id)).size, 6);
    assert.equal(MAX_WINDOW_MINUTES, 7 * DAY_MINUTES);
    assert.ok(isTimeRangeId(DEFAULT_RANGE_ID));
  });

  it("are found by id, and anything else is not an id", () => {
    assert.equal(timeRangeById("7d")?.minutes, 7 * DAY_MINUTES);
    assert.equal(timeRangeById("2w"), null);
    assert.equal(isTimeRangeId("3d"), true);
    assert.equal(isTimeRangeId("3 days"), false);
    assert.equal(isTimeRangeId(undefined), false);
  });

  it("offer the next longer range, and none after the longest", () => {
    assert.equal(longerRange("1h")?.id, "6h");
    assert.equal(longerRange("3d")?.id, "7d");
    assert.equal(longerRange("7d"), null);
  });
});

describe("stepForRange", () => {
  it("picks the smallest step that keeps the window within 1440 steps", () => {
    assert.equal(MAX_WINDOW_STEPS, 1440);
    const steps = TIME_RANGES.map((range) => stepForRange(range.minutes).id);
    assert.deepEqual(steps, ["1m", "1m", "1m", "1m", "5m", "15m"]);
  });

  it("keeps 1-minute steps up to exactly 1440 minutes and moves to 5 minutes one minute later", () => {
    assert.equal(stepForRange(1440).id, "1m");
    assert.equal(stepForRange(1441).id, "5m");
  });

  it("keeps 5-minute steps up to 1440 of them (5 days) and moves to 15 minutes after", () => {
    assert.equal(stepForRange(5 * 1440).id, "5m");
    assert.equal(stepForRange(5 * 1440 + 1).id, "15m");
  });

  it("gives the step's length in minutes", () => {
    assert.equal(stepForRange(3 * DAY_MINUTES).minutes, 5);
    assert.equal(stepForRange(7 * DAY_MINUTES).minutes, 15);
  });

  it("refuses a window longer than 7 days or not a whole number of minutes", () => {
    assert.throws(() => stepForRange(7 * DAY_MINUTES + 1), RangeError);
    assert.throws(() => stepForRange(0), RangeError);
    assert.throws(() => stepForRange(1.5), RangeError);
  });
});

describe("metricsWindow", () => {
  it("ends at the start of the current minute, so the minute still being collected is not asked for", () => {
    const now = Date.parse("2026-10-30T09:15:42.123Z");
    assert.deepEqual(metricsWindow(now, 60), {
      from: "2026-10-30T08:15:00Z",
      to: "2026-10-30T09:15:00Z",
    });
  });

  it("covers 7 days, the longest the API accepts", () => {
    const now = Date.parse("2026-10-30T09:15:00Z");
    assert.deepEqual(metricsWindow(now, 7 * DAY_MINUTES), {
      from: "2026-10-23T09:15:00Z",
      to: "2026-10-30T09:15:00Z",
    });
  });

  it("refuses a window the API would answer 400 for, or a time that is not a number", () => {
    assert.throws(() => metricsWindow(0, 7 * DAY_MINUTES + 1), RangeError);
    assert.throws(() => metricsWindow(0, 0), RangeError);
    assert.throws(() => metricsWindow(Number.NaN, 60), RangeError);
  });
});

describe("previousWindow", () => {
  it("is the same length, ending where the window starts", () => {
    assert.deepEqual(
      previousWindow({
        from: "2026-10-30T08:15:00Z",
        to: "2026-10-30T09:15:00Z",
      }),
      { from: "2026-10-30T07:15:00Z", to: "2026-10-30T08:15:00Z" },
    );
  });

  it("is none when it would start before the 7 days the data is kept", () => {
    assert.equal(
      previousWindow({
        from: "2026-10-26T09:15:00Z",
        to: "2026-10-30T09:15:00Z",
      }),
      null,
    );
    assert.notEqual(
      previousWindow({
        from: "2026-10-27T09:15:00Z",
        to: "2026-10-30T09:15:00Z",
      }),
      null,
    );
  });
});

describe("queries", () => {
  const window = { from: "2026-10-30T08:15:00Z", to: "2026-10-30T09:15:00Z" };

  it("ask for the platform row or one app, with the step", () => {
    assert.deepEqual(metricsQueryFor({ kind: "platform" }, window, "1m"), {
      app: PLATFORM_APP_SLUG,
      ...window,
      step: "1m",
    });
    assert.deepEqual(
      metricsQueryFor({ kind: "app", slug: "sales" }, window, "5m"),
      { app: "sales", ...window, step: "5m" },
    );
  });

  it("ask for the top apps of the breakdown with the same window and step", () => {
    assert.deepEqual(breakdownQueryFor(window, "15m"), {
      ...window,
      step: "15m",
      limit: BREAKDOWN_LIMIT,
    });
    assert.equal(BREAKDOWN_LIMIT, 8);
  });
});

describe("managedApps", () => {
  it("lists the apps someone can manage, by slug, skipping a slug that is not one", () => {
    const apps = [
      app("zeta", "CAN_MANAGE"),
      app("alpha", "CAN_MANAGE"),
      app("view-only", "CAN_USE"),
      app("Bad_Slug", "CAN_MANAGE"),
    ];
    assert.deepEqual(
      managedApps(apps).map((item) => item.slug),
      ["alpha", "zeta"],
    );
  });
});

describe("metricsPollIntervalMs", () => {
  it("polls an hour or less every few seconds", () => {
    assert.equal(metricsPollIntervalMs(15), 5000);
    assert.equal(metricsPollIntervalMs(60), 5000);
  });

  it("polls up to a day every 15 s and longer ranges once a minute: their newest step changes slowly and the answers are large", () => {
    assert.equal(metricsPollIntervalMs(360), 15_000);
    assert.equal(metricsPollIntervalMs(DAY_MINUTES), 15_000);
    assert.equal(metricsPollIntervalMs(3 * DAY_MINUTES), 60_000);
    assert.equal(metricsPollIntervalMs(7 * DAY_MINUTES), 60_000);
  });

  it("never asks for faster than the poll hook allows", () => {
    for (const range of TIME_RANGES) {
      assert.ok(metricsPollIntervalMs(range.minutes) >= MIN_POLL_INTERVAL_MS);
    }
  });
});

describe("stepMinutes", () => {
  it("reads a step of the contract as minutes, so an answer's step lines up with the buckets", () => {
    assert.deepEqual(
      (["1m", "5m", "15m", "1h"] as const).map(stepMinutes),
      [1, 5, 15, 60],
    );
  });
});
