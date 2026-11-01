import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { MetricsResponse, MetricsRow } from "@/lib/api/schemas";
import {
  MEMORY_DANGER_RATIO,
  MEMORY_WARNING_RATIO,
  TONE_TEXTURE,
  buildDashboardModel,
  comparisonCells,
  memoryTone,
} from "@/lib/dashboard/model";
import type {
  ChartTone,
  ColumnChartSpec,
  DashboardModel,
  LineChartSpec,
  SeriesModel,
} from "@/lib/dashboard/model";
import type { DashboardScope } from "@/lib/dashboard/scope";
import { MINUTE_MS } from "@/lib/dashboard/series";
import { readExampleObject } from "../../../support/contract-examples";

const T0 = Date.parse("2026-10-30T08:00:00Z");
const SALES: DashboardScope = { kind: "app", slug: "sales" };
const PLATFORM: DashboardScope = { kind: "platform" };
const APPS: DashboardScope = { kind: "apps" };

function isoAt(offsetMinutes: number): string {
  return new Date(T0 + offsetMinutes * MINUTE_MS)
    .toISOString()
    .replace(".000Z", "Z");
}

/** A row at `T0 + offsetMinutes`, parsed against the real schema, so a test row is a legal one. */
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

const PLATFORM_FIELDS = {
  app_slug: "_platform",
  mem_used_mb: 5775,
  mem_limit_mb: 7168,
  children_running: 3,
  children_registered: 4,
  admission_rejections: 1,
  registry_errors: 0,
  registry_ms_max: 42,
  edge_rss_mb: 180,
};

function response(
  items: MetricsRow[],
  options: { fromMinute?: number; toMinute?: number; truncated?: boolean } = {},
): MetricsResponse {
  return MetricsResponse.parse({
    from: isoAt(options.fromMinute ?? 0),
    to: isoAt(options.toMinute ?? 60),
    truncated: options.truncated ?? false,
    items,
  });
}

function series(model: DashboardModel): SeriesModel {
  assert.equal(model.kind, "series");
  assert.ok(model.kind === "series");
  return model;
}

function columnChart(model: SeriesModel, id: string): ColumnChartSpec {
  const chart = model.charts.find((candidate) => candidate.id === id);
  assert.ok(chart?.kind === "columns", id);
  return chart;
}

function lineChart(model: SeriesModel, id: string): LineChartSpec {
  const chart = model.charts.find((candidate) => candidate.id === id);
  assert.ok(chart?.kind === "line", id);
  return chart;
}

function tile(model: SeriesModel, key: string) {
  const found = model.tiles.find((candidate) => candidate.key === key);
  assert.ok(found !== undefined, key);
  return found;
}

describe("an empty answer", () => {
  it("says there is nothing in the window, with the window, and offers no chart", () => {
    const model = buildDashboardModel(response([]), SALES);
    assert.equal(model.kind, "empty");
    assert.equal(
      model.windowText,
      "2026-10-30 08:00 UTC to 2026-10-30 09:00 UTC",
    );
    assert.deepEqual(model.notices, []);
    assert.ok(model.kind === "empty");
    assert.equal(model.title, "No metrics in this window");
  });

  it("ignores rows of other apps, so one app's page never shows another's numbers", () => {
    const model = buildDashboardModel(
      response([row(10, { app_slug: "ads" })]),
      SALES,
    );
    assert.equal(model.kind, "empty");
  });

  it("still tells that the answer was cut", () => {
    const model = buildDashboardModel(response([], { truncated: true }), SALES);
    assert.equal(model.kind, "empty");
    assert.deepEqual(
      model.notices.map((notice) => notice.key),
      ["truncated"],
    );
  });

  it("is also what an answer with no usable window gives", () => {
    const model = buildDashboardModel(
      MetricsResponse.parse({
        from: isoAt(60),
        to: isoAt(0),
        truncated: false,
        items: [row(5)],
      }),
      SALES,
    );
    assert.equal(model.kind, "empty");
  });
});

describe("one point", () => {
  const model = series(buildDashboardModel(response([row(30)]), SALES));

  it("draws one column at its minute and leaves every other minute as no data", () => {
    const requests = columnChart(model, "requests");
    assert.equal(requests.title, "Requests per minute");
    assert.equal(requests.columns.length, 60);
    assert.deepEqual(requests.columns[30], [400, 9, 3]);
    assert.equal(requests.columns.filter((c) => c !== null).length, 1);
    assert.equal(requests.labels[0], "08:00");
    assert.equal(requests.labels[30], "08:30");
    assert.equal(requests.emptyMessage, null);
  });

  it("gives every column a tooltip that states the figures, and a no-data one says so", () => {
    const requests = columnChart(model, "requests");
    assert.equal(
      requests.tips[30],
      "2026-10-30 08:30 UTC: 412 requests, 9 client errors, 3 server errors",
    );
    assert.equal(requests.tips[29], "2026-10-30 08:29 UTC: no data");
  });

  it("has a table twin with the same figures in words, not colours", () => {
    const requests = columnChart(model, "requests");
    assert.deepEqual(requests.table.headers, [
      "Time",
      "Succeeded (below 400)",
      "Client errors (4xx)",
      "Server errors (5xx)",
      "Total",
    ]);
    assert.equal(requests.table.rows.length, 60);
    assert.deepEqual(requests.table.rows[30]?.cells, [
      "2026-10-30 08:30 UTC",
      "400",
      "9",
      "3",
      "412",
    ]);
    assert.deepEqual(requests.table.rows[29]?.cells, [
      "2026-10-30 08:29 UTC",
      "no data",
      "no data",
      "no data",
      "no data",
    ]);
  });

  it("gives the latency chart a table twin with each bucket's range, count and share, and a tooltip saying the same", () => {
    const latency = columnChart(model, "latency");
    assert.deepEqual(latency.table.headers, [
      "Response time",
      "Requests",
      "Share",
    ]);
    // 412 requests: 18, 61, 140, 88, 52, 29, 14, 6, 3, 1, 0. Shares are n / 412:
    // 4.37%, 14.81%, 33.98%, 21.36%, 12.62%, 7.04%, 3.40%, 1.46%, 0.73%, 0.24%, 0%.
    assert.deepEqual(
      latency.table.rows.map((entry) => entry.cells),
      [
        ["Up to 5 ms", "18", "4.4%"],
        ["Over 5 ms up to 10 ms", "61", "15%"],
        ["Over 10 ms up to 25 ms", "140", "34%"],
        ["Over 25 ms up to 50 ms", "88", "21%"],
        ["Over 50 ms up to 100 ms", "52", "13%"],
        ["Over 100 ms up to 250 ms", "29", "7%"],
        ["Over 250 ms up to 500 ms", "14", "3.4%"],
        ["Over 500 ms up to 1 s", "6", "1.5%"],
        ["Over 1 s up to 2.5 s", "3", "0.7%"],
        ["Over 2.5 s up to 5 s", "1", "0.2%"],
        ["Over 5 s", "0", "0%"],
      ],
    );
    assert.deepEqual(latency.tips, [
      "Up to 5 ms: 18 requests (4.4%)",
      "Over 5 ms up to 10 ms: 61 requests (15%)",
      "Over 10 ms up to 25 ms: 140 requests (34%)",
      "Over 25 ms up to 50 ms: 88 requests (21%)",
      "Over 50 ms up to 100 ms: 52 requests (13%)",
      "Over 100 ms up to 250 ms: 29 requests (7%)",
      "Over 250 ms up to 500 ms: 14 requests (3.4%)",
      "Over 500 ms up to 1 s: 6 requests (1.5%)",
      "Over 1 s up to 2.5 s: 3 requests (0.7%)",
      "Over 2.5 s up to 5 s: 1 request (0.2%)",
      "Over 5 s: 0 requests (0%)",
    ]);
  });

  it("gives the memory chart a table twin and tooltips: a value per minute, and 'no data' for a minute with no row", () => {
    const memory = lineChart(model, "memory");
    assert.deepEqual(memory.table.headers, ["Time", "Memory (MB)"]);
    assert.equal(memory.table.rows.length, 60);
    assert.deepEqual(memory.table.rows[30]?.cells, [
      "2026-10-30 08:30 UTC",
      "341",
    ]);
    assert.deepEqual(memory.table.rows[29]?.cells, [
      "2026-10-30 08:29 UTC",
      "no data",
    ]);
    assert.equal(memory.tips[30], "2026-10-30 08:30 UTC: 341 MB");
    assert.equal(memory.tips[29], "2026-10-30 08:29 UTC: no data");
  });

  it("gives the egress chart a table twin and tooltips: zero drops is a '0', a minute with no row is 'no data'", () => {
    const egress = columnChart(model, "egress");
    assert.deepEqual(egress.table.headers, ["Time", "Drops"]);
    assert.equal(egress.table.rows.length, 60);
    assert.deepEqual(egress.table.rows[30]?.cells, [
      "2026-10-30 08:30 UTC",
      "0",
    ]);
    assert.deepEqual(egress.table.rows[29]?.cells, [
      "2026-10-30 08:29 UTC",
      "no data",
    ]);
    assert.equal(egress.tips[30], "2026-10-30 08:30 UTC: 0 drops");
    assert.equal(egress.tips[29], "2026-10-30 08:29 UTC: no data");
  });

  it("names the three kinds of response in the legend, in the order they stack", () => {
    const requests = columnChart(model, "requests");
    assert.deepEqual(
      requests.legend.map((item) => item.label),
      ["Succeeded (below 400)", "Client errors (4xx)", "Server errors (5xx)"],
    );
    assert.deepEqual(requests.tones, ["primary", "warning", "danger"]);
  });

  it("reads the percentiles from the histogram (design worked example: p50 in 25 ms, p95 in 500 ms)", () => {
    assert.equal(tile(model, "p50").value, "at most 25 ms");
    assert.equal(tile(model, "p95").value, "at most 500 ms");
    const latency = columnChart(model, "latency");
    assert.equal(latency.columns.length, 11);
    assert.deepEqual(latency.columns[2], [140]);
    assert.deepEqual(latency.labels.slice(0, 2), ["5", "10"]);
    assert.equal(latency.labels[10], "> 5,000");
    assert.deepEqual(latency.highlights, [
      { index: 2, label: "p50" },
      { index: 6, label: "p95" },
    ]);
  });

  it("merges the labels of both percentiles when they fall in the same bucket", () => {
    const one = series(
      buildDashboardModel(
        response([row(30, { lat_hist: [0, 0, 9, 0, 0, 0, 0, 0, 0, 0, 0] })]),
        SALES,
      ),
    );
    assert.deepEqual(columnChart(one, "latency").highlights, [
      { index: 2, label: "p50, p95" },
    ]);
  });

  it("draws the app's memory as one point, with no limit line (the API gives none for an app)", () => {
    const memory = lineChart(model, "memory");
    assert.equal(memory.title, "Memory peak per minute");
    assert.equal(memory.values.length, 60);
    assert.equal(memory.values[30], 341);
    assert.equal(memory.values.filter((v) => v !== null).length, 1);
    assert.equal(memory.reference, null);
    assert.equal(tile(model, "memory").value, "341 MB");
    assert.equal(tile(model, "memory").meter, null);
  });

  it("counts a minute with zero drops as a column of zero, not as a gap", () => {
    const egress = columnChart(model, "egress");
    assert.deepEqual(egress.columns[30], [0]);
    assert.equal(egress.columns[29], null);
    assert.equal(egress.emptyMessage, "No packet was dropped in this window.");
    assert.equal(tile(model, "egress").value, "0");
  });

  it("states the requests tile with the average over the minutes that have data", () => {
    assert.equal(tile(model, "requests").value, "412");
    assert.equal(
      tile(model, "requests").detail,
      "412 per minute over 1 minute with data",
    );
    assert.equal(tile(model, "errors").value, "3");
    assert.equal(tile(model, "errors").detail, "0.7% of requests");
  });

  it("shows a small share with a decimal and a big one as a whole percent", () => {
    const rare = series(
      buildDashboardModel(
        response([row(5, { requests: 100000, r4xx: 0, r5xx: 4 })]),
        SALES,
      ),
    );
    assert.equal(tile(rare, "errors").detail, "less than 0.1% of requests");
    const common = series(
      buildDashboardModel(
        response([row(5, { requests: 200, r4xx: 0, r5xx: 50 })]),
        SALES,
      ),
    );
    assert.equal(tile(common, "errors").detail, "25% of requests");
    const none = series(
      buildDashboardModel(
        response([row(5, { requests: 200, r4xx: 0, r5xx: 0 })]),
        SALES,
      ),
    );
    assert.equal(tile(none, "errors").detail, "0% of requests");
  });

  it("raises no notice: one row has no interior gap", () => {
    assert.deepEqual(model.notices, []);
  });
});

describe("rows outside the window", () => {
  it("do not reach the tiles, the drops or the facts of an app", () => {
    // The answer says it covers 08:00 to 09:00; a row at 07:55 or 09:30 is not part of it.
    const model = series(
      buildDashboardModel(
        response([
          row(-5, {
            requests: 1000,
            r5xx: 900,
            egress_dropped: 9,
            egress_dropped_by_rule: { "app:9": 9 },
            restarts: 5,
          }),
          row(10),
          row(90, { requests: 2000, r5xx: 800, egress_dropped: 8 }),
        ]),
        SALES,
      ),
    );
    assert.equal(tile(model, "requests").value, "412");
    assert.equal(tile(model, "errors").value, "3");
    assert.equal(tile(model, "egress").value, "0");
    assert.deepEqual(model.drops, []);
    assert.equal(
      model.facts.find((fact) => fact.key === "restarts")?.value,
      "0",
    );
    // No interior gap is invented between 07:55, 08:10 and 09:30.
    assert.deepEqual(model.notices, []);
  });

  it("do not make an app of the comparison table", () => {
    const model = buildDashboardModel(
      response([row(-5, { app_slug: "ghost" }), row(10)]),
      APPS,
    );
    assert.ok(model.kind === "apps");
    assert.deepEqual(
      model.rows.map((entry) => entry.slug),
      ["sales"],
    );
    assert.equal(model.rows[0]?.requests, 412);
  });
});

describe("other series in the same answer", () => {
  it("never reach the drops, tiles or facts of the scope on screen", () => {
    const rows = [
      row(10, {
        egress_dropped: 2,
        egress_dropped_by_rule: { "app:1": 2 },
        restarts: 1,
      }),
      row(10, {
        app_slug: "ads",
        requests: 5000,
        egress_dropped: 50,
        egress_dropped_by_rule: { "app:7": 50 },
        restarts: 6,
      }),
    ];
    const model = series(buildDashboardModel(response(rows), SALES));
    assert.deepEqual(model.drops, [{ rule: "app:1", count: 2 }]);
    assert.equal(tile(model, "requests").value, "412");
    assert.equal(tile(model, "egress").value, "2");
    assert.equal(
      model.facts.find((fact) => fact.key === "restarts")?.value,
      "1",
    );
  });
});

describe("a gap", () => {
  it("reports the missing minutes between two rows, but not the minutes before the first or after the last", () => {
    const model = buildDashboardModel(
      response([row(10), row(15), row(50)]),
      SALES,
    );
    assert.deepEqual(
      model.notices.map((notice) => [notice.key, notice.tone, notice.title]),
      [["gap", "info", "38 minutes without data"]],
    );
    const gap = model.notices[0];
    assert.ok(gap !== undefined);
    assert.match(gap.detail, /stopped/);
  });

  it("says 1 minute for one", () => {
    const model = buildDashboardModel(response([row(10), row(12)]), SALES);
    assert.equal(model.notices[0]?.title, "1 minute without data");
  });

  it("draws the stretch as no data between the points, which the chart geometry reads as a gap", () => {
    const model = series(
      buildDashboardModel(response([row(10), row(12)]), SALES),
    );
    const memory = lineChart(model, "memory");
    assert.equal(memory.values[10], 341);
    assert.equal(memory.values[11], null);
    assert.equal(memory.values[12], 341);
  });
});

describe("a cut answer", () => {
  it("warns first, names the new start of the window and what to do", () => {
    const model = buildDashboardModel(
      response([row(30), row(40)], { fromMinute: 20, truncated: true }),
      SALES,
    );
    assert.deepEqual(
      model.notices.map((notice) => notice.key),
      ["truncated", "gap"],
    );
    const cut = model.notices[0];
    assert.ok(cut !== undefined);
    assert.equal(cut.tone, "warning");
    assert.match(cut.detail, /2026-10-30 08:20 UTC/);
    assert.match(cut.detail, /one app or a shorter range/);
    assert.equal(
      model.windowText,
      "2026-10-30 08:20 UTC to 2026-10-30 09:00 UTC",
    );
  });

  it("draws only the window the answer says it covers", () => {
    const model = series(
      buildDashboardModel(
        response([row(30)], { fromMinute: 20, truncated: true }),
        SALES,
      ),
    );
    assert.equal(columnChart(model, "requests").columns.length, 40);
  });

  it("does not warn when the flag is false", () => {
    const model = buildDashboardModel(response([row(30)]), SALES);
    assert.deepEqual(model.notices, []);
  });
});

describe("large counts", () => {
  const model = series(
    buildDashboardModel(
      response([
        row(5, { requests: 12_345_678, r2xx: 12_345_000, r4xx: 600, r5xx: 78 }),
        row(6, { requests: 7, r2xx: 7, r3xx: 0, r4xx: 0, r5xx: 0 }),
      ]),
      SALES,
    ),
  );

  it("groups digits in the tiles and the tooltips", () => {
    assert.equal(tile(model, "requests").value, "12,345,685");
    assert.match(
      columnChart(model, "requests").tips[5] ?? "",
      /^2026-10-30 08:05 UTC: 12,345,678 requests, 600 client errors, 78 server errors$/,
    );
  });

  it("averages over the minutes with data and rounds a big rate to a whole number", () => {
    assert.equal(
      tile(model, "requests").detail,
      "6,172,843 per minute over 2 minutes with data",
    );
  });

  it("stacks segments that add up to the total, even when other statuses exist", () => {
    const weird = series(
      buildDashboardModel(
        response([
          row(5, { requests: 10, r2xx: 1, r3xx: 0, r4xx: 2, r5xx: 3 }),
        ]),
        SALES,
      ),
    );
    // 10 requests, 2 + 3 are errors: the other 5 are "succeeded" whatever the 1xx/2xx/3xx split.
    assert.deepEqual(columnChart(weird, "requests").columns[5], [5, 2, 3]);
  });

  it("never lets a segment go negative when the counts disagree", () => {
    const odd = series(
      buildDashboardModel(
        response([row(5, { requests: 2, r4xx: 2, r5xx: 3 })]),
        SALES,
      ),
    );
    assert.deepEqual(columnChart(odd, "requests").columns[5], [0, 2, 3]);
  });
});

describe("long windows", () => {
  it("add minutes into buckets and say so in the title", () => {
    const day = buildDashboardModel(
      MetricsResponse.parse({
        from: "2026-10-29T09:00:00Z",
        to: "2026-10-30T09:00:00Z",
        truncated: false,
        items: [row(-60), row(-59)],
      }),
      SALES,
    );
    const model = series(day);
    const requests = columnChart(model, "requests");
    assert.equal(requests.title, "Requests per 20 minutes");
    assert.equal(requests.columns.length, 72);
    // Both rows fall in the 07:00 bucket: counts add up.
    const bucket = requests.columns.find((column) => column !== null);
    assert.deepEqual(bucket, [800, 18, 6]);
    assert.equal(requests.labels[0], "10-29 09:00");
    assert.equal(memoryTitle(model), "Memory peak per 20 minutes");
  });

  it("reads a bucket's table rows and tooltips as sums for counts and the peak for memory", () => {
    const model = series(
      buildDashboardModel(
        MetricsResponse.parse({
          from: "2026-10-29T09:00:00Z",
          to: "2026-10-30T09:00:00Z",
          truncated: false,
          items: [
            row(-60, {
              egress_dropped: 2,
              rss_mb_max: 300,
              lat_hist: [18, 61, 140, 88, 52, 29, 14, 6, 3, 1, 0],
            }),
            row(-59, {
              egress_dropped: 3,
              rss_mb_max: 341,
              lat_hist: [2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 4],
            }),
          ],
        }),
        SALES,
      ),
    );
    // 72 buckets of 20 minutes end at 09:00, so the one that starts at 07:00 is
    // number (24 h - 2 h) / 20 min = 66; the one before starts at 06:40.
    const egress = columnChart(model, "egress");
    assert.equal(egress.title, "Egress drops per 20 minutes");
    assert.deepEqual(egress.table.rows[66]?.cells, [
      "2026-10-30 07:00 UTC",
      "5",
    ]);
    assert.equal(egress.tips[66], "2026-10-30 07:00 UTC, 20 minutes: 5 drops");
    assert.equal(egress.tips[65], "2026-10-30 06:40 UTC, 20 minutes: no data");
    assert.deepEqual(egress.table.rows[65]?.cells, [
      "2026-10-30 06:40 UTC",
      "no data",
    ]);

    const memory = lineChart(model, "memory");
    assert.equal(memory.values[66], 341);
    assert.deepEqual(memory.table.rows[66]?.cells, [
      "2026-10-30 07:00 UTC",
      "341",
    ]);
    assert.equal(memory.tips[66], "2026-10-30 07:00 UTC, 20 minutes: 341 MB");

    // The histogram is the two minutes added bucket by bucket: 20, 61, 140, ..., 4.
    const latency = columnChart(model, "latency");
    assert.deepEqual(
      latency.table.rows.map((entry) => entry.cells[1]),
      ["20", "61", "140", "88", "52", "29", "14", "6", "3", "1", "4"],
    );
    // 418 requests: 20 is 4.78%, 4 is 0.96%.
    assert.equal(latency.table.rows[0]?.cells[2], "4.8%");
    assert.deepEqual(latency.table.rows[10]?.cells, ["Over 5 s", "4", "1%"]);
    assert.equal(latency.tips[10], "Over 5 s: 4 requests (1%)");
  });

  it("marks a bucket that starts before the window as partial", () => {
    const model = series(
      buildDashboardModel(
        response([row(0)], { fromMinute: 0, toMinute: 73 }),
        SALES,
      ),
    );
    const requests = columnChart(model, "requests");
    assert.equal(requests.columns.length, 37);
    assert.match(requests.tips[0] ?? "", /\(partial\)/);
    assert.doesNotMatch(requests.tips[1] ?? "", /partial/);
  });
});

function memoryTitle(model: SeriesModel): string {
  return lineChart(model, "memory").title;
}

describe("the platform", () => {
  const rows = [
    row(10, PLATFORM_FIELDS),
    row(11, {
      ...PLATFORM_FIELDS,
      mem_used_mb: 6000,
      egress_dropped: 4,
      egress_dropped_by_rule: { "app:2": 3, "global:1": 1 },
    }),
    row(12, { app_slug: "ads" }),
  ];
  const model = series(buildDashboardModel(response(rows), PLATFORM));

  it("uses only the platform rows", () => {
    assert.equal(tile(model, "requests").value, "824");
  });

  it("draws memory used against the limit and keeps the limit on the chart", () => {
    const memory = lineChart(model, "memory");
    assert.equal(memory.values[10], 5775);
    assert.equal(memory.values[11], 6000);
    assert.equal(memory.reference, 7168);
    assert.equal(memory.referenceLabel, "Memory limit");
  });

  it("gives the memory line and the limit tones of their own, which the legend repeats", () => {
    const memory = lineChart(model, "memory");
    assert.notEqual(memory.tone, memory.referenceTone);
    assert.deepEqual(
      memory.legend.map((item) => item.tone),
      [memory.tone, memory.referenceTone],
    );
  });

  it("names the used memory and the limit in the legend and lists both in the memory table", () => {
    const memory = lineChart(model, "memory");
    assert.deepEqual(memory.legend, [
      { label: "Memory used", tone: "primary" },
      { label: "Memory limit", tone: "danger" },
    ]);
    assert.deepEqual(memory.table.headers, [
      "Time",
      "Memory (MB)",
      "Limit (MB)",
    ]);
    assert.deepEqual(memory.table.rows[10]?.cells, [
      "2026-10-30 08:10 UTC",
      "5,775",
      "7,168",
    ]);
    assert.deepEqual(memory.table.rows[11]?.cells, [
      "2026-10-30 08:11 UTC",
      "6,000",
      "7,168",
    ]);
    assert.deepEqual(memory.table.rows[12]?.cells, [
      "2026-10-30 08:12 UTC",
      "no data",
      "no data",
    ]);
    assert.equal(memory.tips[11], "2026-10-30 08:11 UTC: 6,000 MB");
    assert.equal(memory.tips[12], "2026-10-30 08:12 UTC: no data");
  });

  it("lists the drops of each minute in the egress table, and counts packets, which is what the API counts", () => {
    const egress = columnChart(model, "egress");
    assert.equal(egress.emptyMessage, null);
    assert.deepEqual(egress.table.headers, ["Time", "Drops"]);
    assert.deepEqual(egress.table.rows[10]?.cells, [
      "2026-10-30 08:10 UTC",
      "0",
    ]);
    assert.deepEqual(egress.table.rows[11]?.cells, [
      "2026-10-30 08:11 UTC",
      "4",
    ]);
    assert.deepEqual(egress.table.rows[12]?.cells, [
      "2026-10-30 08:12 UTC",
      "no data",
    ]);
    assert.equal(egress.tips[11], "2026-10-30 08:11 UTC: 4 drops");
    assert.equal(egress.tips[10], "2026-10-30 08:10 UTC: 0 drops");
    assert.equal(egress.tips[12], "2026-10-30 08:12 UTC: no data");
    assert.match(
      egress.description,
      /^4 packets dropped by egress rules in the window\. /,
    );
    assert.equal(
      tile(model, "egress").detail,
      "packets dropped by egress rules",
    );
  });

  it("shows the peak as a meter in words and in numbers", () => {
    const peak = tile(model, "memory");
    assert.equal(peak.value, "6,000 MB");
    assert.equal(peak.detail, "84% of the 7,168 MB limit");
    assert.deepEqual(peak.meter, {
      label: "Platform memory peak",
      value: 6000,
      max: 7168,
      tone: "warning",
    });
  });

  it("lists the drops by rule, most first", () => {
    assert.deepEqual(model.drops, [
      { rule: "app:2", count: 3 },
      { rule: "global:1", count: 1 },
    ]);
    assert.equal(tile(model, "egress").value, "4");
  });

  it("adds the platform's own facts: apps running, refused starts, registry, edge memory", () => {
    const facts = new Map(model.facts.map((fact) => [fact.key, fact.value]));
    assert.equal(facts.get("children"), "3 of 4 running");
    assert.equal(facts.get("admission"), "2");
    assert.equal(facts.get("registry-errors"), "0");
    assert.equal(facts.get("registry-slowest"), "42 ms");
    assert.equal(facts.get("edge-memory"), "180 MB");
  });

  it("leaves those facts out for an app", () => {
    const app = series(buildDashboardModel(response([row(5)]), SALES));
    const keys = app.facts.map((fact) => fact.key);
    assert.ok(keys.includes("restarts"));
    assert.ok(!keys.includes("children"));
    assert.ok(!keys.includes("edge-memory"));
  });

  it("has no meter when the rows carry no memory figure", () => {
    const bare = series(
      buildDashboardModel(
        response([
          row(10, {
            app_slug: "_platform",
            mem_used_mb: null,
            mem_limit_mb: null,
          }),
        ]),
        PLATFORM,
      ),
    );
    assert.equal(tile(bare, "memory").meter, null);
    assert.equal(tile(bare, "memory").value, "341 MB");
    assert.equal(lineChart(bare, "memory").reference, null);
  });
});

describe("TONE_TEXTURE", () => {
  it("gives each tone of a chart a pattern of its own, so a stacked segment is never told apart by colour alone", () => {
    const model = series(buildDashboardModel(response([row(30)]), SALES));
    const requests = columnChart(model, "requests");
    const textures = requests.tones.map((tone) => TONE_TEXTURE[tone]);
    assert.equal(new Set(textures).size, requests.tones.length);
    // The plain tone is the one with no pattern; the other two have one each.
    assert.equal(TONE_TEXTURE.primary, "none");
    assert.deepEqual(textures, ["none", "stripes", "dots"]);
  });

  it("is the same for the legend, which reads the tones of the legend items", () => {
    const model = series(buildDashboardModel(response([row(30)]), SALES));
    const requests = columnChart(model, "requests");
    const legendTones: ChartTone[] = requests.legend.map((item) => item.tone);
    assert.deepEqual(legendTones, requests.tones);
  });
});

describe("memoryTone", () => {
  it("is green below the warning ratio, amber from it, red from the danger ratio", () => {
    assert.ok(MEMORY_WARNING_RATIO < MEMORY_DANGER_RATIO);
    assert.equal(memoryTone(0, 1000), "success");
    assert.equal(memoryTone(749, 1000), "success");
    assert.equal(memoryTone(750, 1000), "warning");
    assert.equal(memoryTone(899, 1000), "warning");
    assert.equal(memoryTone(900, 1000), "danger");
    assert.equal(memoryTone(5000, 1000), "danger");
  });

  it("does not guess without a limit", () => {
    assert.equal(memoryTone(500, 0), "success");
    assert.equal(memoryTone(500, Number.NaN), "success");
  });
});

describe("a latency histogram with no request", () => {
  it("says nothing was measured instead of drawing empty bars, and the percentiles read 'no requests'", () => {
    const model = series(
      buildDashboardModel(
        response([
          row(5, {
            requests: 0,
            r2xx: 0,
            r3xx: 0,
            r4xx: 0,
            r5xx: 0,
            lat_hist: new Array<number>(11).fill(0),
          }),
        ]),
        SALES,
      ),
    );
    const latency = columnChart(model, "latency");
    assert.equal(
      latency.emptyMessage,
      "No request was measured in this window.",
    );
    assert.deepEqual(latency.highlights, []);
    assert.equal(tile(model, "p50").value, "no requests");
    assert.equal(
      columnChart(model, "requests").emptyMessage,
      "No requests in this window.",
    );
    assert.equal(tile(model, "errors").detail, "no requests");
  });
});

describe("the apps comparison", () => {
  const rows = [
    row(5),
    row(6),
    row(5, { app_slug: "ads", requests: 1000, r5xx: 0 }),
    row(5, PLATFORM_FIELDS),
  ];
  const model = buildDashboardModel(response(rows), APPS);

  it("lists apps busiest first and leaves the platform row out", () => {
    assert.equal(model.kind, "apps");
    assert.ok(model.kind === "apps");
    assert.deepEqual(
      model.rows.map((entry) => entry.slug),
      ["ads", "sales"],
    );
  });

  it("formats a row as text, with the percentile as the bucket's upper bound", () => {
    assert.ok(model.kind === "apps");
    const sales = model.rows.find((entry) => entry.slug === "sales");
    assert.ok(sales !== undefined);
    assert.deepEqual(comparisonCells(sales), {
      requests: "824",
      serverErrors: "6",
      p50: "at most 25 ms",
      p95: "at most 500 ms",
      memory: "341 MB",
      egressDrops: "0",
      restarts: "0",
    });
  });

  it("is empty when no app wrote a row", () => {
    const none = buildDashboardModel(response([row(5, PLATFORM_FIELDS)]), APPS);
    assert.equal(none.kind, "empty");
  });
});
