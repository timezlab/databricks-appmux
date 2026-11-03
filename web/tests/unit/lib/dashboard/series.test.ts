import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { MetricsRow } from "@/lib/api/schemas";
import {
  BUCKET_MINUTE_CHOICES,
  LATENCY_BUCKET_BOUNDS_MS,
  LATENCY_BUCKET_COUNT,
  MAX_COLUMNS,
  MINUTE_MS,
  bucketRows,
  chooseBucketMinutes,
  describeLatency,
  dropsByRule,
  latencyBucketLabel,
  lengthText,
  perText,
  percentileBucket,
  rowsInDomain,
  stepText,
  summarizeRows,
  windowDomain,
} from "@/lib/dashboard/series";
import { MAX_WINDOW_MINUTES } from "@/lib/dashboard/scope";
import { readExampleObject } from "../../../support/contract-examples";

const T0 = Date.parse("2026-10-30T09:00:00Z");

function isoAt(offsetMinutes: number): string {
  return new Date(T0 + offsetMinutes * MINUTE_MS)
    .toISOString()
    .replace(".000Z", "Z");
}

/** An app row at `T0 + offsetMinutes`, parsed against the real schema, so a test row is a legal one. */
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

const HOUR = windowDomain(isoAt(0), isoAt(60));
assert.ok(HOUR !== null);

describe("the latency buckets", () => {
  it("are the 10 upper bounds of the data model plus the overflow bucket, as many as the schema has", () => {
    assert.deepEqual(
      [...LATENCY_BUCKET_BOUNDS_MS],
      [5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000],
    );
    assert.equal(LATENCY_BUCKET_COUNT, LATENCY_BUCKET_BOUNDS_MS.length + 1);
    assert.equal(row(0).lat_hist.length, LATENCY_BUCKET_COUNT);
  });
});

describe("windowDomain", () => {
  it("is the window from the response, in whole minutes", () => {
    assert.deepEqual(
      windowDomain("2026-10-30T09:00:00Z", "2026-10-30T10:00:00Z"),
      {
        startMs: T0,
        endMs: T0 + 60 * MINUTE_MS,
      },
    );
  });

  it("widens a window that does not sit on minute boundaries, so no minute of it is cut", () => {
    assert.deepEqual(
      windowDomain("2026-10-30T09:00:30Z", "2026-10-30T09:10:10Z"),
      {
        startMs: T0,
        endMs: T0 + 11 * MINUTE_MS,
      },
    );
  });

  it("is null when the answer has no usable window", () => {
    assert.equal(windowDomain("soon", "2026-10-30T10:00:00Z"), null);
    assert.equal(
      windowDomain("2026-10-30T10:00:00Z", "2026-10-30T10:00:00Z"),
      null,
    );
    assert.equal(
      windowDomain("2026-10-30T11:00:00Z", "2026-10-30T10:00:00Z"),
      null,
    );
  });

  it("keeps the newest 7 days of a window that is longer than the API allows", () => {
    const domain = windowDomain("2026-10-01T00:00:00Z", "2026-10-30T09:00:00Z");
    assert.deepEqual(domain, {
      startMs: T0 - MAX_WINDOW_MINUTES * MINUTE_MS,
      endMs: T0,
    });
  });
});

describe("chooseBucketMinutes", () => {
  it("shows every minute while they fit, and widens the bucket when they do not", () => {
    assert.equal(MAX_COLUMNS, 72);
    const minutes = (count: number) => {
      const domain = windowDomain(isoAt(0), isoAt(count));
      assert.ok(domain !== null);
      return chooseBucketMinutes(domain);
    };
    assert.equal(minutes(15), 1);
    assert.equal(minutes(60), 1);
    assert.equal(minutes(72), 1);
    assert.equal(minutes(73), 2);
    assert.equal(minutes(360), 5);
    assert.equal(minutes(1440), 20);
    assert.equal(minutes(3 * 1440), 60);
    assert.equal(minutes(7 * 1440), 180);
  });

  it("counts the buckets on UTC boundaries, so a window that starts mid-bucket may need a wider one", () => {
    const day = windowDomain(isoAt(7), isoAt(7 + 1440));
    assert.ok(day !== null);
    // 20-minute buckets from 09:00 to 09:20 the next day are 73: one too many.
    assert.equal(chooseBucketMinutes(day), 30);
  });

  it("only uses widths that hold whole steps", () => {
    const week = windowDomain(isoAt(0), isoAt(7 * 1440));
    assert.ok(week !== null);
    assert.equal(chooseBucketMinutes(week, MAX_COLUMNS, 15) % 15, 0);
    const threeDays = windowDomain(isoAt(0), isoAt(3 * 1440));
    assert.ok(threeDays !== null);
    assert.equal(chooseBucketMinutes(threeDays, MAX_COLUMNS, 5) % 5, 0);
    const short = windowDomain(isoAt(0), isoAt(30));
    assert.ok(short !== null);
    assert.equal(chooseBucketMinutes(short, MAX_COLUMNS, 5), 5);
  });

  it("never needs more columns than allowed for any window the API returns", () => {
    for (let slots = 1; slots <= MAX_WINDOW_MINUTES; slots += 7) {
      const domain = windowDomain(isoAt(3), isoAt(3 + slots));
      assert.ok(domain !== null);
      const size = chooseBucketMinutes(domain);
      assert.ok(BUCKET_MINUTE_CHOICES.some((choice) => choice === size));
      const series = bucketRows([], domain);
      assert.ok(
        series.buckets.length <= MAX_COLUMNS,
        `${slots} minutes, size ${size}, ${series.buckets.length} columns`,
      );
    }
  });
});

describe("stepText", () => {
  it("names a bucket width for a caption", () => {
    assert.equal(stepText(1), "1-minute");
    assert.equal(stepText(20), "20-minute");
    assert.equal(stepText(60), "1-hour");
    assert.equal(stepText(180), "3-hour");
    assert.equal(stepText(1440), "1-day");
  });
});

describe("lengthText and perText", () => {
  it("say a bucket's length in its largest whole unit, for a tooltip and a title", () => {
    assert.equal(lengthText(1), "1 minute");
    assert.equal(lengthText(20), "20 minutes");
    assert.equal(lengthText(60), "1 hour");
    assert.equal(lengthText(180), "3 hours");
    assert.equal(lengthText(1440), "1 day");
    assert.equal(perText(1), "minute");
    assert.equal(perText(20), "20 minutes");
    assert.equal(perText(60), "hour");
    // 7 days in 72 columns at most: 3-hour buckets, never "180 minutes".
    assert.equal(perText(180), "3 hours");
    assert.equal(perText(1440), "day");
  });
});

describe("bucketRows", () => {
  it("makes one bucket per minute of an hour, empty ones included, oldest first", () => {
    const series = bucketRows([], HOUR, MAX_COLUMNS);
    assert.equal(series.bucketMinutes, 1);
    assert.equal(series.buckets.length, 60);
    assert.equal(series.buckets[0]?.startMs, T0);
    assert.equal(series.buckets[59]?.startMs, T0 + 59 * MINUTE_MS);
    assert.ok(
      series.buckets.every(
        (bucket) => !bucket.hasData && bucket.requests === 0,
      ),
    );
  });

  it("puts a row in the bucket of its minute", () => {
    const series = bucketRows([row(15, { requests: 412, r5xx: 3 })], HOUR);
    const bucket = series.buckets[15];
    assert.equal(bucket?.hasData, true);
    assert.equal(bucket?.requests, 412);
    assert.equal(bucket?.r5xx, 3);
    assert.equal(bucket?.reportedMinutes, 1);
    assert.equal(series.buckets.filter((b) => b.hasData).length, 1);
  });

  it("tells a minute with zero requests (the app ran) from a minute with no row (a gap)", () => {
    const series = bucketRows(
      [row(1, { requests: 0, r2xx: 0, r3xx: 0, r4xx: 0, r5xx: 0 })],
      HOUR,
    );
    assert.equal(series.buckets[1]?.hasData, true);
    assert.equal(series.buckets[1]?.requests, 0);
    assert.equal(series.buckets[2]?.hasData, false);
  });

  it("adds counts and takes the peak of peaks inside a wider bucket", () => {
    const day = windowDomain(isoAt(0), isoAt(1440));
    assert.ok(day !== null);
    const series = bucketRows(
      [
        row(0, { requests: 10, rss_mb_max: 300, egress_dropped: 1 }),
        row(19, { requests: 5, rss_mb_max: 340, egress_dropped: 2 }),
        row(20, { requests: 7, rss_mb_max: 100 }),
      ],
      day,
    );
    assert.equal(series.bucketMinutes, 20);
    assert.equal(series.buckets.length, 72);
    assert.equal(series.buckets[0]?.requests, 15);
    assert.equal(series.buckets[0]?.rssMbMax, 340);
    assert.equal(series.buckets[0]?.egressDropped, 3);
    assert.equal(series.buckets[0]?.reportedMinutes, 2);
    assert.equal(series.buckets[0]?.coveredMinutes, 20);
    assert.equal(series.buckets[0]?.isPartial, false);
    assert.equal(series.buckets[1]?.requests, 7);
  });

  it("adds the latency histograms bucket by bucket", () => {
    const day = windowDomain(isoAt(0), isoAt(1440));
    assert.ok(day !== null);
    const series = bucketRows(
      [
        row(0, { lat_hist: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11] }),
        row(1, { lat_hist: [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1] }),
      ],
      day,
    );
    assert.deepEqual(
      series.buckets[0]?.latHist,
      [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
    );
  });

  it("starts the buckets on UTC boundaries and marks the first and the last when the window cuts them", () => {
    const sevenMinutes = windowDomain(isoAt(3), isoAt(10));
    assert.ok(sevenMinutes !== null);
    const series = bucketRows(
      [row(3, { requests: 1 }), row(9, { requests: 2 })],
      sevenMinutes,
      3,
    );
    assert.equal(series.bucketMinutes, 5);
    assert.equal(series.buckets.length, 2);
    assert.equal(series.buckets[0]?.startMs, T0);
    assert.equal(series.buckets[0]?.coveredMinutes, 2);
    assert.equal(series.buckets[0]?.isPartial, true);
    assert.equal(series.buckets[0]?.requests, 1);
    assert.equal(series.buckets[1]?.coveredMinutes, 5);
    assert.equal(series.buckets[1]?.isPartial, false);
    assert.equal(series.buckets[1]?.requests, 2);

    const growing = windowDomain(isoAt(0), isoAt(7));
    assert.ok(growing !== null);
    const last = bucketRows([], growing, 3).buckets.at(-1);
    assert.equal(last?.coveredMinutes, 2);
    assert.equal(last?.isPartial, true);
  });

  it("keeps a step the window cuts at its start (the contract's rule) in the first bucket", () => {
    // A 15-minute step from 09:00 in a window that starts at 09:07.
    const window = windowDomain(isoAt(7), isoAt(7 + 3 * 1440));
    assert.ok(window !== null);
    const series = bucketRows(
      [row(0, { requests: 5 }), row(15, { requests: 6 })],
      window,
      MAX_COLUMNS,
      15,
    );
    assert.equal(series.buckets[0]?.requests, 11);
  });

  it("ignores a row outside the window or with a minute that is not a time", () => {
    const series = bucketRows(
      [
        row(-1, { requests: 99 }),
        row(60, { requests: 99 }),
        row(5, { requests: 4 }),
        { ...row(6, { requests: 99 }), minute: "not a time" },
      ],
      HOUR,
    );
    assert.equal(
      series.buckets.reduce((sum, bucket) => sum + bucket.requests, 0),
      4,
    );
  });

  it("keeps the platform fields null in a bucket of app rows and fills them from platform rows", () => {
    const app = bucketRows([row(0)], HOUR).buckets[0];
    assert.equal(app?.memUsedMbMax, null);
    assert.equal(app?.memLimitMb, null);
    const platform = bucketRows(
      [
        row(0, {
          app_slug: "_platform",
          mem_used_mb: 1500,
          mem_limit_mb: 6144,
          children_running: 1,
          children_registered: 2,
          admission_rejections: 0,
          registry_errors: 0,
          registry_ms_max: 12,
          edge_rss_mb: 160,
        }),
      ],
      HOUR,
    ).buckets[0];
    assert.equal(platform?.memUsedMbMax, 1500);
    assert.equal(platform?.memLimitMb, 6144);
    assert.equal(platform?.childrenRunning, 1);
    assert.equal(platform?.childrenRegistered, 2);
    assert.equal(app?.childrenRunning, null);
  });

  it("keeps the newest child counts of a wider bucket, as the server keeps the latest minute of a step", () => {
    const day = windowDomain(isoAt(0), isoAt(1440));
    assert.ok(day !== null);
    const platform = (offset: number, running: number, registered: number) =>
      row(offset, {
        app_slug: "_platform",
        mem_used_mb: 1500,
        mem_limit_mb: 6144,
        children_running: running,
        children_registered: registered,
      });
    const bucket = bucketRows([platform(5, 3, 4), platform(1, 1, 2)], day)
      .buckets[0];
    assert.equal(bucket?.childrenRunning, 3);
    assert.equal(bucket?.childrenRegistered, 4);
  });

  it("adds large counts without losing a unit", () => {
    const day = windowDomain(isoAt(0), isoAt(1440));
    assert.ok(day !== null);
    const rows = Array.from({ length: 1440 }, (_, minute) =>
      row(minute, {
        requests: 2_000_000_000,
        r2xx: 2_000_000_000,
        r3xx: 0,
        r4xx: 0,
        r5xx: 0,
      }),
    );
    const series = bucketRows(rows, day);
    assert.equal(series.buckets[0]?.requests, 20 * 2_000_000_000);
    const total = series.buckets.reduce(
      (sum, bucket) => sum + bucket.requests,
      0,
    );
    assert.equal(total, 1440 * 2_000_000_000);
  });
});

describe("rowsInDomain", () => {
  it("keeps the rows of the window, oldest first", () => {
    const rows = rowsInDomain([row(30), row(-5), row(2), row(60)], HOUR);
    assert.deepEqual(
      rows.map((r) => r.minute),
      [isoAt(2), isoAt(30)],
    );
  });

  it("keeps a step that starts before the window but reaches into it", () => {
    const window = windowDomain(isoAt(7), isoAt(67));
    assert.ok(window !== null);
    const rows = rowsInDomain([row(0), row(-15), row(60)], window, 15);
    assert.deepEqual(
      rows.map((r) => r.minute),
      [isoAt(0), isoAt(60)],
    );
  });
});

describe("summarizeRows", () => {
  it("is all zeros and nulls for no rows", () => {
    const summary = summarizeRows([]);
    assert.equal(summary.reportedMinutes, 0);
    assert.equal(summary.requests, 0);
    assert.equal(summary.firstMinuteMs, null);
    assert.equal(summary.lastMinuteMs, null);
    assert.equal(summary.gapMinutes, 0);
    assert.equal(summary.memUsedMbMax, null);
    assert.equal(summary.childrenRunning, null);
    assert.deepEqual(
      summary.latHist,
      new Array<number>(LATENCY_BUCKET_COUNT).fill(0),
    );
  });

  it("adds counts, takes peaks, and sums the histograms of a single row", () => {
    const summary = summarizeRows([row(15)]);
    assert.equal(summary.requests, 412);
    assert.equal(summary.r2xx, 389);
    assert.equal(summary.r5xx, 3);
    assert.equal(summary.upstreamErrors, 2);
    assert.equal(summary.rssMbMax, 341);
    assert.equal(summary.inFlightMax, 7);
    assert.equal(summary.reportedMinutes, 1);
    assert.equal(summary.firstMinuteMs, T0 + 15 * MINUTE_MS);
    assert.equal(summary.lastMinuteMs, T0 + 15 * MINUTE_MS);
    assert.equal(summary.gapMinutes, 0);
    assert.deepEqual(summary.latHist, row(15).lat_hist);
  });

  it("adds the counts of several minutes and takes the highest peak, whichever minute holds it", () => {
    // Every field differs from row to row, so a sum cannot pass for "the last
    // row" or "the first row", and a peak cannot pass for a sum, a minimum or
    // the last value. The peaks sit in the first (rss) and middle (in flight)
    // rows on purpose.
    const rows = [
      row(10, {
        requests: 100,
        r2xx: 80,
        r3xx: 5,
        r4xx: 10,
        r5xx: 5,
        upstream_errors: 1,
        limited: 2,
        lat_hist: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11],
        bytes_in: 1000,
        bytes_out: 2000,
        in_flight_max: 4,
        ws_opened: 1,
        rss_mb_max: 300,
        restarts: 1,
        egress_bytes_out: 10,
        egress_bytes_in: 20,
        egress_dropped: 3,
      }),
      row(11, {
        requests: 200,
        r2xx: 150,
        r3xx: 20,
        r4xx: 20,
        r5xx: 10,
        upstream_errors: 2,
        limited: 4,
        lat_hist: [0, 0, 5, 0, 0, 0, 0, 0, 0, 0, 1],
        bytes_in: 3000,
        bytes_out: 4000,
        in_flight_max: 9,
        ws_opened: 2,
        rss_mb_max: 250,
        restarts: 2,
        egress_bytes_out: 100,
        egress_bytes_in: 200,
        egress_dropped: 5,
      }),
      row(13, {
        requests: 300,
        r2xx: 270,
        r3xx: 10,
        r4xx: 15,
        r5xx: 5,
        upstream_errors: 4,
        limited: 8,
        lat_hist: [10, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2],
        bytes_in: 5000,
        bytes_out: 6000,
        in_flight_max: 6,
        ws_opened: 4,
        rss_mb_max: 280,
        restarts: 4,
        egress_bytes_out: 1000,
        egress_bytes_in: 2000,
        egress_dropped: 7,
      }),
    ];
    const summary = summarizeRows(rows);
    assert.equal(summary.requests, 600);
    assert.equal(summary.r2xx, 500);
    assert.equal(summary.r3xx, 35);
    assert.equal(summary.r4xx, 45);
    assert.equal(summary.r5xx, 20);
    assert.equal(summary.upstreamErrors, 7);
    assert.equal(summary.limited, 14);
    assert.equal(summary.bytesIn, 9000);
    assert.equal(summary.bytesOut, 12_000);
    assert.equal(summary.wsOpened, 7);
    assert.equal(summary.restarts, 7);
    assert.equal(summary.egressDropped, 15);
    assert.equal(summary.egressBytesOut, 1110);
    assert.equal(summary.egressBytesIn, 2220);
    assert.equal(summary.inFlightMax, 9);
    assert.equal(summary.rssMbMax, 300);
    assert.deepEqual(summary.latHist, [11, 2, 8, 4, 5, 6, 7, 8, 9, 10, 14]);
    // Minutes 10, 11 and 13: three rows, one missing minute (12) between them.
    assert.equal(summary.reportedMinutes, 3);
    assert.equal(summary.gapMinutes, 1);
    assert.equal(summary.firstMinuteMs, T0 + 10 * MINUTE_MS);
    assert.equal(summary.lastMinuteMs, T0 + 13 * MINUTE_MS);
  });

  it("counts the minutes missing between the first and the last row, and not those before or after", () => {
    const summary = summarizeRows([
      row(10),
      row(11),
      row(15),
      row(16),
      row(17),
    ]);
    assert.equal(summary.reportedMinutes, 5);
    assert.equal(summary.gapMinutes, 3);
  });

  it("counts missing steps, not minutes, when the rows are steps longer than a minute", () => {
    const summary = summarizeRows([row(0), row(15), row(45)], 15);
    assert.equal(summary.reportedMinutes, 45);
    assert.equal(summary.gapMinutes, 15);
  });

  it("takes the peaks, the limit and the newest child counts from platform rows", () => {
    const platform = (offset: number, used: number, running: number) =>
      row(offset, {
        app_slug: "_platform",
        mem_used_mb: used,
        mem_limit_mb: 6144,
        children_running: running,
        children_registered: 3,
        admission_rejections: 1,
        registry_errors: 2,
        registry_ms_max: offset * 10,
        edge_rss_mb: 160 + offset,
      });
    const summary = summarizeRows([
      platform(0, 1500, 1),
      platform(2, 1800, 3),
      platform(1, 1200, 2),
    ]);
    assert.equal(summary.memUsedMbMax, 1800);
    assert.equal(summary.memLimitMb, 6144);
    assert.equal(summary.childrenRunning, 3);
    assert.equal(summary.childrenRegistered, 3);
    assert.equal(summary.admissionRejections, 3);
    assert.equal(summary.registryErrors, 6);
    assert.equal(summary.registryMsMax, 20);
    assert.equal(summary.edgeRssMbMax, 162);
  });
});

describe("percentileBucket (design 10.2: the bucket the Nth request falls in)", () => {
  // The worked example of the data model: running totals 18, 79, 219, 307 ...
  const HIST = [18, 61, 140, 88, 52, 29, 14, 6, 3, 1, 0];

  it("puts the p50 of 412 requests in the third bucket, at most 25 ms", () => {
    assert.equal(percentileBucket(HIST, 50), 2);
    assert.equal(describeLatency(2), "at most 25 ms");
  });

  it("finds the p95 and the p100", () => {
    assert.equal(percentileBucket(HIST, 95), 6);
    assert.equal(percentileBucket(HIST, 100), 9);
  });

  it("is null when nothing was measured", () => {
    assert.equal(percentileBucket(new Array<number>(11).fill(0), 50), null);
    assert.equal(percentileBucket([], 50), null);
    assert.equal(describeLatency(null), "no requests");
  });

  it("reads the slowest bucket as 'over 5 s' and one request as every percentile", () => {
    const slow = new Array<number>(11).fill(0);
    slow[10] = 1;
    assert.equal(percentileBucket(slow, 1), 10);
    assert.equal(percentileBucket(slow, 99), 10);
    assert.equal(describeLatency(10), "over 5 s");
  });

  it("is exact on whole ranks (100 requests: the p95 is the 95th)", () => {
    const hist = new Array<number>(11).fill(0);
    hist[0] = 95;
    hist[1] = 5;
    assert.equal(percentileBucket(hist, 95), 0);
    hist[0] = 94;
    hist[1] = 6;
    assert.equal(percentileBucket(hist, 95), 1);
  });

  it("rounds a fractional rank up: 10 requests, the p95 is the 10th (rank 9.5), not the 9th", () => {
    const hist = new Array<number>(11).fill(0);
    hist[0] = 9;
    hist[1] = 1;
    assert.equal(percentileBucket(hist, 95), 1);
    // The 9th request is still the fast one: the p90 is exactly rank 9.
    assert.equal(percentileBucket(hist, 90), 0);
  });

  it("rounds up, not to the nearest: a rank just above a whole number takes the next request", () => {
    // 10 requests, the first 5 in bucket 0. p50 is exactly the 5th (rank 5.0);
    // p51 is rank 5.1, which is the 6th request, in bucket 1. Rounding to the
    // nearest or down would keep it in bucket 0.
    const hist = new Array<number>(11).fill(0);
    hist[0] = 5;
    hist[1] = 5;
    assert.equal(percentileBucket(hist, 50), 0);
    assert.equal(percentileBucket(hist, 51), 1);
    assert.equal(percentileBucket(hist, 100), 1);
  });

  it("takes the first request for the lowest percentile, even when 1% of the requests is a fraction", () => {
    // 250 requests, p1 is rank 2.5 -> the 3rd, which is in bucket 1.
    const hist = new Array<number>(11).fill(0);
    hist[0] = 2;
    hist[1] = 248;
    assert.equal(percentileBucket(hist, 1), 1);
  });

  it("multiplies before it divides: 7% of 100 is exactly rank 7 (100 * (7 / 100) is 7.000000000000001)", () => {
    const hist = new Array<number>(11).fill(0);
    hist[0] = 7;
    hist[1] = 93;
    assert.equal(percentileBucket(hist, 7), 0);
  });

  it("refuses a percentile outside 1 to 100", () => {
    for (const percentile of [0, -1, 101, Number.NaN]) {
      assert.throws(() => percentileBucket(HIST, percentile), RangeError);
    }
  });

  it("names the buckets by their upper bound", () => {
    assert.equal(latencyBucketLabel(0), "5");
    assert.equal(latencyBucketLabel(7), "1,000");
    assert.equal(latencyBucketLabel(9), "5,000");
    assert.equal(latencyBucketLabel(10), "> 5,000");
    assert.equal(describeLatency(7), "at most 1 s");
    assert.equal(describeLatency(8), "at most 2.5 s");
  });
});

describe("dropsByRule", () => {
  it("adds the drops of each rule over the window, most first, ties by rule id", () => {
    const rows = [
      row(0, {
        egress_dropped: 5,
        egress_dropped_by_rule: { "app:2": 3, "global:1": 2 },
      }),
      row(1, {
        egress_dropped: 4,
        egress_dropped_by_rule: { "app:2": 1, "app:1": 3 },
      }),
    ];
    assert.deepEqual(dropsByRule(rows), [
      { rule: "app:2", count: 4 },
      { rule: "app:1", count: 3 },
      { rule: "global:1", count: 2 },
    ]);
  });

  it("is empty when nothing was dropped, and skips zero counts", () => {
    assert.deepEqual(dropsByRule([row(0)]), []);
    assert.deepEqual(
      dropsByRule([row(0, { egress_dropped_by_rule: { "app:1": 0 } })]),
      [],
    );
  });

  it("treats a rule id as plain text, whatever it spells", () => {
    // Built by hand: JSON.parse makes "__proto__" an own key, as a response body can.
    const hostile: MetricsRow = {
      ...row(0),
      egress_dropped_by_rule: JSON.parse('{"__proto__": 2}') as Record<
        string,
        number
      >,
    };
    const [first] = dropsByRule([hostile]);
    assert.deepEqual(first, { rule: "__proto__", count: 2 });
  });
});
