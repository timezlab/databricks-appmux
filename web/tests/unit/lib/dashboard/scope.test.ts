import assert from "node:assert/strict";
import { describe, it } from "vitest";

import type { AppSummary } from "@/lib/api/schemas";
import {
  DEFAULT_RANGE_ID,
  MAX_WINDOW_MINUTES,
  PLATFORM_APP_SLUG,
  TIME_RANGES,
  defaultScopeKey,
  isTimeRangeId,
  metricsPollIntervalMs,
  metricsQueryFor,
  metricsWindow,
  parseScopeKey,
  resolveScopeKey,
  scopeKey,
  scopeOptions,
  timeRangeById,
} from "@/lib/dashboard/scope";
import { MIN_POLL_INTERVAL_MS } from "@/lib/poll/schedule";

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
  it("are ordered, unique, within the 1440-minute window the API allows, and include the default", () => {
    const minutes = TIME_RANGES.map((range) => range.minutes);
    assert.deepEqual(
      minutes,
      minutes.toSorted((a, b) => a - b),
    );
    assert.equal(
      new Set(TIME_RANGES.map((range) => range.id)).size,
      minutes.length,
    );
    assert.ok(
      minutes.every((value) => value >= 1 && value <= MAX_WINDOW_MINUTES),
    );
    assert.equal(MAX_WINDOW_MINUTES, 1440);
    assert.ok(isTimeRangeId(DEFAULT_RANGE_ID));
  });

  it("are found by id, and anything else is not an id", () => {
    assert.equal(timeRangeById("6h")?.minutes, 360);
    assert.equal(timeRangeById("7d"), null);
    assert.equal(timeRangeById(""), null);
    assert.equal(isTimeRangeId("24h"), true);
    assert.equal(isTimeRangeId("__proto__"), false);
    assert.equal(isTimeRangeId(60), false);
  });
});

describe("metricsWindow", () => {
  it("ends at the start of the current minute, so the minute still being collected is not asked for", () => {
    const now = Date.parse("2026-10-30T09:16:41.250Z");
    assert.deepEqual(metricsWindow(now, 60), {
      from: "2026-10-30T08:16:00Z",
      to: "2026-10-30T09:16:00Z",
    });
  });

  it("sends whole seconds with a Z, the form of the contract's examples", () => {
    const { from, to } = metricsWindow(
      Date.parse("2026-10-30T00:00:00Z"),
      1440,
    );
    assert.equal(from, "2026-10-29T00:00:00Z");
    assert.equal(to, "2026-10-30T00:00:00Z");
  });

  it("refuses a window the API would answer 400 for", () => {
    for (const minutes of [
      0,
      -5,
      1441,
      1.5,
      Number.NaN,
      Number.POSITIVE_INFINITY,
    ]) {
      assert.throws(
        () => metricsWindow(0, minutes),
        RangeError,
        String(minutes),
      );
    }
  });

  it("refuses a time that is not a number", () => {
    assert.throws(() => metricsWindow(Number.NaN, 60), RangeError);
  });
});

describe("scope keys", () => {
  it("round-trip", () => {
    for (const scope of [
      { kind: "platform" },
      { kind: "apps" },
      { kind: "app", slug: "sales" },
    ] as const) {
      assert.deepEqual(parseScopeKey(scopeKey(scope)), scope);
    }
    assert.equal(scopeKey({ kind: "app", slug: "sales" }), "app:sales");
  });

  it("refuse a slug that does not match the contract pattern, so it never reaches a query", () => {
    for (const key of [
      "app:",
      "app:Sales",
      "app:../x",
      "app:a/b",
      "app:_platform",
      "app:sales ",
      "apps:",
      "platform ",
      "other",
      "",
    ]) {
      assert.equal(parseScopeKey(key), null, JSON.stringify(key));
    }
  });
});

describe("metricsQueryFor", () => {
  const window = { from: "2026-10-30T08:00:00Z", to: "2026-10-30T09:00:00Z" };

  it("asks for the platform row alone, for every app, or for one app", () => {
    assert.deepEqual(metricsQueryFor({ kind: "platform" }, window), {
      app: PLATFORM_APP_SLUG,
      ...window,
    });
    assert.deepEqual(metricsQueryFor({ kind: "apps" }, window), window);
    assert.deepEqual(metricsQueryFor({ kind: "app", slug: "sales" }, window), {
      app: "sales",
      ...window,
    });
  });
});

describe("scopeOptions", () => {
  it("gives a platform admin the platform, the comparison and every app by slug, whatever the app permission says", () => {
    const options = scopeOptions({
      isPlatformAdmin: true,
      apps: [app("sales", "CAN_USE"), app("ads", "CAN_MANAGE")],
    });
    assert.deepEqual(
      options.map((option) => option.key),
      ["platform", "apps", "app:ads", "app:sales"],
    );
  });

  it("gives anyone else only the apps they can manage", () => {
    const options = scopeOptions({
      isPlatformAdmin: false,
      apps: [app("sales", "CAN_USE"), app("ads", "CAN_MANAGE")],
    });
    assert.deepEqual(
      options.map((option) => option.key),
      ["app:ads"],
    );
  });

  it("gives someone who manages nothing no option", () => {
    assert.deepEqual(
      scopeOptions({ isPlatformAdmin: false, apps: [app("sales", "CAN_USE")] }),
      [],
    );
  });

  it("skips an app whose slug is not a slug, and shows the name as text with the slug", () => {
    const options = scopeOptions({
      isPlatformAdmin: false,
      apps: [app("../x", "CAN_MANAGE"), app("sales", "CAN_MANAGE")],
    });
    assert.equal(options.length, 1);
    assert.equal(options[0]?.label, "App sales (sales)");
  });
});

describe("defaultScopeKey and resolveScopeKey", () => {
  const admin = scopeOptions({
    isPlatformAdmin: true,
    apps: [app("sales", "CAN_MANAGE")],
  });
  const owner = scopeOptions({
    isPlatformAdmin: false,
    apps: [app("sales", "CAN_MANAGE"), app("ads", "CAN_MANAGE")],
  });

  it("start a platform admin on the platform and an app admin on their first app", () => {
    assert.equal(defaultScopeKey(admin), "platform");
    assert.equal(defaultScopeKey(owner), "app:ads");
    assert.equal(defaultScopeKey([]), null);
  });

  it("keep the choice while it is offered and fall back when it is gone (the app was deleted or the role lost)", () => {
    assert.equal(resolveScopeKey("app:sales", owner), "app:sales");
    assert.equal(resolveScopeKey("app:gone", owner), "app:ads");
    assert.equal(resolveScopeKey("platform", owner), "app:ads");
    assert.equal(resolveScopeKey(null, admin), "platform");
    assert.equal(resolveScopeKey("platform", []), null);
  });
});

describe("metricsPollIntervalMs", () => {
  it("polls a small answer every few seconds", () => {
    assert.equal(metricsPollIntervalMs({ kind: "platform" }, 15), 5000);
    assert.equal(
      metricsPollIntervalMs({ kind: "app", slug: "sales" }, 60),
      5000,
    );
  });

  it("polls a big answer less often: the data changes once a minute and 1440 rows or every app's rows are large", () => {
    assert.equal(
      metricsPollIntervalMs({ kind: "app", slug: "sales" }, 360),
      15_000,
    );
    assert.equal(metricsPollIntervalMs({ kind: "platform" }, 1440), 15_000);
    assert.equal(metricsPollIntervalMs({ kind: "apps" }, 15), 15_000);
  });

  it("never asks for faster than the poll hook allows", () => {
    for (const range of TIME_RANGES) {
      for (const scope of [
        { kind: "platform" },
        { kind: "apps" },
        { kind: "app", slug: "sales" },
      ] as const) {
        assert.ok(
          metricsPollIntervalMs(scope, range.minutes) >= MIN_POLL_INTERVAL_MS,
        );
      }
    }
  });
});
