import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { MetricsBreakdown } from "@/lib/api/schemas";
import type { MetricsPoint } from "@/lib/api/schemas";
import {
  BREAKDOWN_OUTLINE,
  OTHER_APPS_LABEL,
  buildBreakdownModel,
} from "@/lib/dashboard/breakdown";
import type { BreakdownModel } from "@/lib/dashboard/breakdown";
import type { ColumnChartSpec } from "@/lib/dashboard/model";
import { MINUTE_MS } from "@/lib/dashboard/series";
import { readExample } from "../../../support/contract-examples";

/** The contract example: 09:00 to 09:40 at 15 minutes, "docs" and "old-reports", and the rest as `other`. */
const EXAMPLE = MetricsBreakdown.parse(readExample("MetricsBreakdown"));

function breakdown(response: MetricsBreakdown): BreakdownModel {
  const model = buildBreakdownModel(response);
  assert.ok(model.kind === "breakdown");
  return model;
}

function chart(model: BreakdownModel, id: string): ColumnChartSpec {
  const found = model.charts.find((candidate) => candidate.id === id);
  assert.ok(found !== undefined, id);
  return found;
}

const T0 = Date.parse("2026-10-30T09:00:00Z");

function point(
  offsetMinutes: number,
  overrides: Partial<MetricsPoint> = {},
): MetricsPoint {
  return {
    minute: new Date(T0 + offsetMinutes * MINUTE_MS)
      .toISOString()
      .replace(".000Z", "Z"),
    requests: 10,
    r4xx: 1,
    r5xx: 0,
    rss_mb_max: 100,
    bytes_out: 0,
    egress_dropped: 0,
    lat_hist: [10, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    ...overrides,
  };
}

function response(
  series: { slug: string; points: MetricsPoint[] }[],
  other: MetricsPoint[] | null = null,
  window: { from: string; to: string } = {
    from: "2026-10-30T09:00:00Z",
    to: "2026-10-30T10:00:00Z",
  },
): MetricsBreakdown {
  return MetricsBreakdown.parse({
    ...window,
    step: "1m",
    series: series.map(({ slug, points }) => ({
      app_slug: slug,
      display_name: null,
      points,
    })),
    other,
  });
}

describe("the breakdown of the contract example", () => {
  const model = breakdown(EXAMPLE);

  it("stacks the apps in slug order, the apps below the cut on top, one tone each in every chart", () => {
    for (const id of ["requests-by-app", "memory-by-app", "errors-by-app"]) {
      const spec = chart(model, id);
      assert.deepEqual(spec.legend, [
        { label: "Docs Portal", tone: "series-1", slug: "docs" },
        { label: "old-reports", tone: "series-2", slug: "old-reports" },
        { label: OTHER_APPS_LABEL, tone: "other" },
      ]);
      assert.deepEqual(spec.tones, ["series-1", "series-2", "other"]);
    }
  });

  it("adds each app's steps into the buckets of the window, a step with no point drawn as nothing", () => {
    const requests = chart(model, "requests-by-app");
    assert.equal(requests.title, "Requests by app, per 15 minutes");
    assert.equal(requests.xCaption, "15-minute totals, time in UTC");
    assert.deepEqual(requests.columns, [
      [6120, 1210, 802],
      [5875, 0, 788],
      [3904, 0, 0],
    ]);
    assert.deepEqual(requests.labels, ["09:00", "09:15", "09:30"]);
  });

  it("writes each column's tooltip top of the stack first, leaves out an app with nothing, and ends with the total", () => {
    const requests = chart(model, "requests-by-app");
    assert.equal(
      requests.tips[0],
      [
        "2026-10-30 09:00 UTC, 15 minutes",
        "Other apps: 802",
        "old-reports: 1,210",
        "Docs Portal: 6,120",
        "Total: 8,132",
      ].join("\n"),
    );
    assert.equal(
      requests.tips[2],
      "2026-10-30 09:30 UTC (partial)\nDocs Portal: 3,904\nTotal: 3,904",
    );
  });

  it("has a table twin with a column per app and the total, 'no data' where an app wrote no step", () => {
    const requests = chart(model, "requests-by-app");
    assert.deepEqual(requests.table.headers, [
      "Time",
      "Docs Portal",
      "old-reports",
      "Other apps",
      "Total",
    ]);
    assert.deepEqual(requests.table.rows[1]?.cells, [
      "2026-10-30 09:15 UTC",
      "5,875",
      "no data",
      "788",
      "6,663",
    ]);
  });

  it("stacks the memory peaks, and says the sum is one of peaks", () => {
    const memory = chart(model, "memory-by-app");
    assert.equal(memory.title, "Memory peak by app, per 15 minutes");
    assert.equal(
      memory.xCaption,
      "Each app's 15-minute peak, stacked; time in UTC",
    );
    assert.deepEqual(memory.columns[0], [412, 690, 520]);
    assert.match(memory.tips[0] ?? "", /\nSum of peaks: 1,622 MB$/);
    assert.equal(memory.table.headers[1], "Docs Portal (MB)");
  });

  it("draws the charts its outline names, in that order", () => {
    assert.deepEqual(
      model.charts.map((spec) => spec.id),
      BREAKDOWN_OUTLINE.map((outline) => outline.id),
    );
  });

  it("stacks the server errors of each app", () => {
    const errors = chart(model, "errors-by-app");
    assert.equal(errors.title, "Server errors by app, per 15 minutes");
    assert.deepEqual(errors.columns, [
      [3, 30, 0],
      [0, 0, 1],
      [1, 0, 0],
    ]);
    assert.equal(errors.emptyMessage, null);
  });

  it("lists the apps busiest first, the apps below the cut last, with their window figures", () => {
    assert.deepEqual(model.appHeaders, [
      "App",
      "Requests",
      "Client errors (4xx)",
      "Server errors (5xx)",
      "Response time, p95",
      "Memory peak",
      "Egress drops",
    ]);
    assert.deepEqual(
      model.apps.map((app) => [app.slug, app.label, app.tone, app.cells]),
      [
        [
          "docs",
          "Docs Portal",
          "series-1",
          ["15,899", "99", "4", "at most 250 ms", "418 MB", "0"],
        ],
        [
          "old-reports",
          "old-reports",
          "series-2",
          ["1,210", "12", "30", "at most 500 ms", "690 MB", "4"],
        ],
        [
          null,
          OTHER_APPS_LABEL,
          "other",
          ["1,590", "9", "1", "at most 250 ms", "534 MB", "2"],
        ],
      ],
    );
  });
});

describe("the tones", () => {
  it("follow the slug, not the rank, so a busier app does not repaint the others", () => {
    const model = breakdown(
      response([
        { slug: "zeta", points: [point(0, { requests: 500 })] },
        { slug: "alpha", points: [point(0)] },
      ]),
    );
    assert.deepEqual(
      chart(model, "requests-by-app").legend.map((item) => [
        item.slug,
        item.tone,
      ]),
      [
        ["alpha", "series-1"],
        ["zeta", "series-2"],
      ],
    );
    assert.deepEqual(
      model.apps.map((app) => app.slug),
      ["zeta", "alpha"],
    );
  });

  it("never go past the eighth: a ninth app is added to the apps below the cut, memory peaks summed", () => {
    const series = Array.from({ length: 10 }, (_, index) => ({
      slug: `app-${String(index).padStart(2, "0")}`,
      points: [point(0, { requests: 100 - index, rss_mb_max: 10 })],
    }));
    const model = breakdown(response(series, [point(0, { rss_mb_max: 50 })]));
    const requests = chart(model, "requests-by-app");
    assert.equal(requests.legend.length, 9);
    assert.deepEqual(requests.tones, [
      "series-1",
      "series-2",
      "series-3",
      "series-4",
      "series-5",
      "series-6",
      "series-7",
      "series-8",
      "other",
    ]);
    // The other apps (10) and the two cut ones (92 and 91 requests).
    assert.equal(requests.columns[0]?.[8], 10 + 92 + 91);
    assert.equal(chart(model, "memory-by-app").columns[0]?.[8], 50 + 10 + 10);
  });
});

describe("steps against the window", () => {
  it("keeps a step the window cuts at its start and leaves out one wholly before it", () => {
    const model = breakdown(
      MetricsBreakdown.parse({
        from: "2026-10-30T09:05:00Z",
        to: "2026-10-30T09:40:00Z",
        step: "15m",
        series: [
          {
            app_slug: "docs",
            display_name: null,
            points: [point(-15, { requests: 999 }), point(0), point(15)],
          },
        ],
        other: null,
      }),
    );
    const requests = chart(model, "requests-by-app");
    assert.deepEqual(requests.columns, [[10], [10], null]);
    assert.match(requests.tips[0] ?? "", /^2026-10-30 09:05 UTC \(partial\)/);
    assert.equal(model.apps[0]?.cells[0], "20");
  });
});

describe("an empty breakdown", () => {
  it("is empty when no app has a series and there is no rest", () => {
    const model = buildBreakdownModel(response([], null));
    assert.equal(model.kind, "empty");
  });

  it("says a chart has nothing to draw when nothing of its kind happened", () => {
    const model = breakdown(response([{ slug: "docs", points: [point(0)] }]));
    assert.equal(
      chart(model, "errors-by-app").emptyMessage,
      "No server error in this window.",
    );
    assert.equal(chart(model, "requests-by-app").emptyMessage, null);
  });
});
