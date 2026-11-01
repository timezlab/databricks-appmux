import assert from "node:assert/strict";
import { describe, it } from "vitest";

import {
  UNKNOWN_TEXT,
  describeTime,
  formatAbsoluteTime,
  formatBytes,
  formatCount,
  formatDate,
  formatDurationMs,
  formatMegabytes,
  formatPercent,
  formatRelativeTime,
  parseTime,
  pluralize,
  toIsoUtc,
} from "@/lib/format";

const SECOND_MS = 1000;
const MINUTE_MS = 60 * SECOND_MS;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;

const THEN = "2026-10-30T09:14:00Z";
const THEN_MS = Date.UTC(2026, 9, 30, 9, 14, 0);

describe("parseTime", () => {
  it("reads the contract's time (ISO 8601 UTC, trailing Z)", () => {
    assert.equal(parseTime(THEN), THEN_MS);
    assert.equal(parseTime("2026-10-30T09:14:00.250Z"), THEN_MS + 250);
  });

  it("passes epoch milliseconds through", () => {
    assert.equal(parseTime(THEN_MS), THEN_MS);
  });

  it("refuses what is not a UTC time of the contract, so a wrong zone is never shown as UTC", () => {
    for (const bad of [
      "",
      "yesterday",
      "2026-10-30",
      "2026-10-30 09:14:00Z",
      "2026-10-30T09:14:00",
      "2026-10-30T09:14:00+02:00",
      "2026-10-30T09:14Z",
      "2026-13-01T00:00:00Z",
    ]) {
      assert.equal(parseTime(bad), null, bad);
    }
  });

  it("refuses a date that does not exist instead of rolling it over", () => {
    assert.equal(parseTime("2026-02-30T00:00:00Z"), null);
    assert.equal(parseTime("2026-10-30T24:00:00Z"), null);
  });

  it("refuses a number that is not a time", () => {
    assert.equal(parseTime(Number.NaN), null);
    assert.equal(parseTime(Number.POSITIVE_INFINITY), null);
  });

  it("refuses a number outside the years 0000 to 9999 that the contract's text can write, so no formatter throws or prints NaN", () => {
    const FIRST_VALID_MS = -62_167_219_200_000; // 0000-01-01T00:00:00Z
    const LAST_VALID_MS = 253_402_300_799_999; // 9999-12-31T23:59:59.999Z
    assert.equal(parseTime(FIRST_VALID_MS), FIRST_VALID_MS);
    assert.equal(parseTime(LAST_VALID_MS), LAST_VALID_MS);
    assert.equal(parseTime(FIRST_VALID_MS - 1), null);
    assert.equal(parseTime(LAST_VALID_MS + 1), null);
    assert.equal(parseTime(1e20), null);
    assert.equal(parseTime(-1e20), null);
    assert.equal(parseTime(8_640_000_000_000_001), null);
  });

  it("agrees with the text form at both ends of the range", () => {
    assert.equal(parseTime("0000-01-01T00:00:00Z"), -62_167_219_200_000);
    assert.equal(parseTime("9999-12-31T23:59:59.999Z"), 253_402_300_799_999);
  });
});

describe("toIsoUtc", () => {
  it("is the machine-readable form for a <time dateTime> attribute", () => {
    assert.equal(toIsoUtc(THEN), "2026-10-30T09:14:00.000Z");
    assert.equal(toIsoUtc(THEN_MS), "2026-10-30T09:14:00.000Z");
  });

  it("is null for a value that is not a time, so no invalid dateTime is written", () => {
    assert.equal(toIsoUtc("nonsense"), null);
    assert.equal(toIsoUtc(Number.NaN), null);
  });

  it("is null, and does not throw, for a number beyond the range of a date", () => {
    assert.equal(toIsoUtc(1e20), null);
    assert.equal(toIsoUtc(-1e20), null);
  });
});

describe("formatAbsoluteTime", () => {
  it("writes the UTC date and minute, with the zone named", () => {
    assert.equal(formatAbsoluteTime(THEN), "2026-10-30 09:14 UTC");
  });

  it("can add the seconds", () => {
    assert.equal(
      formatAbsoluteTime("2026-10-30T08:00:03Z", { seconds: true }),
      "2026-10-30 08:00:03 UTC",
    );
  });

  it("does not depend on the machine's zone: the same instant, the same text", () => {
    assert.equal(
      formatAbsoluteTime("2026-01-01T00:00:00Z"),
      "2026-01-01 00:00 UTC",
    );
    assert.equal(
      formatAbsoluteTime("2026-12-31T23:59:59Z"),
      "2026-12-31 23:59 UTC",
    );
  });

  it("says unknown for a value that is not a time", () => {
    assert.equal(formatAbsoluteTime("nonsense"), UNKNOWN_TEXT);
  });

  it("says unknown, never NaN, for a number beyond the range of a date", () => {
    assert.equal(formatAbsoluteTime(1e20), UNKNOWN_TEXT);
    assert.equal(formatAbsoluteTime(-1e20), UNKNOWN_TEXT);
  });
});

describe("formatDate", () => {
  it("is the UTC calendar date", () => {
    assert.equal(formatDate(THEN), "2026-10-30");
    assert.equal(formatDate("nonsense"), UNKNOWN_TEXT);
  });
});

describe("formatRelativeTime", () => {
  const now = THEN_MS;
  const ago = (ms: number) => new Date(now - ms).toISOString();

  it("says just now within ten seconds, in either direction (client and server clocks differ)", () => {
    assert.equal(formatRelativeTime(ago(0), now), "just now");
    assert.equal(formatRelativeTime(ago(9 * SECOND_MS), now), "just now");
    assert.equal(formatRelativeTime(ago(-9 * SECOND_MS), now), "just now");
  });

  it("counts seconds, minutes, hours and days, rounding down", () => {
    assert.equal(
      formatRelativeTime(ago(10 * SECOND_MS), now),
      "10 seconds ago",
    );
    assert.equal(
      formatRelativeTime(ago(59 * SECOND_MS), now),
      "59 seconds ago",
    );
    assert.equal(formatRelativeTime(ago(MINUTE_MS), now), "1 minute ago");
    assert.equal(
      formatRelativeTime(ago(3 * MINUTE_MS + 59 * SECOND_MS), now),
      "3 minutes ago",
    );
    assert.equal(formatRelativeTime(ago(HOUR_MS), now), "1 hour ago");
    assert.equal(
      formatRelativeTime(ago(23 * HOUR_MS + 59 * MINUTE_MS), now),
      "23 hours ago",
    );
    assert.equal(formatRelativeTime(ago(DAY_MS), now), "1 day ago");
    assert.equal(formatRelativeTime(ago(29 * DAY_MS), now), "29 days ago");
  });

  it("looks forward the same way", () => {
    assert.equal(formatRelativeTime(ago(-2 * MINUTE_MS), now), "in 2 minutes");
    assert.equal(formatRelativeTime(ago(-HOUR_MS), now), "in 1 hour");
  });

  it("falls back to the date once a month has passed", () => {
    assert.equal(formatRelativeTime(ago(30 * DAY_MS), now), "2026-09-30");
    assert.equal(formatRelativeTime("2025-01-02T03:04:05Z", now), "2025-01-02");
    assert.equal(formatRelativeTime(ago(-30 * DAY_MS), now), "2026-11-29");
  });

  it("accepts epoch milliseconds, as the poll hook records them", () => {
    assert.equal(formatRelativeTime(now - 5 * MINUTE_MS, now), "5 minutes ago");
  });

  it("says unknown for a value that is not a time", () => {
    assert.equal(formatRelativeTime("soon", now), UNKNOWN_TEXT);
  });
});

describe("describeTime (what RelativeTime draws)", () => {
  const now = THEN_MS;

  it("gives the machine-readable form, the hover text and the shown text", () => {
    assert.deepEqual(describeTime(THEN, now + 2 * SECOND_MS), {
      iso: "2026-10-30T09:14:00.000Z",
      absolute: "2026-10-30 09:14 UTC",
      text: "just now",
    });
    assert.equal(
      describeTime(THEN, now + 3 * MINUTE_MS)?.text,
      "3 minutes ago",
    );
  });

  it("shows the absolute time while there is no 'now' (the static prerender)", () => {
    assert.equal(describeTime(THEN, null)?.text, "2026-10-30 09:14 UTC");
  });

  it("reads a row created seconds ago as just now, not as a time in the future", () => {
    // RelativeTime passes the clock of the moment of rendering (clock-ticker.ts).
    const createdAt = now;
    assert.equal(
      describeTime(createdAt, createdAt + 2 * SECOND_MS)?.text,
      "just now",
    );
    assert.equal(
      describeTime(createdAt, createdAt + 12 * SECOND_MS)?.text,
      "12 seconds ago",
    );
  });

  it("looks forward for a time that is really ahead", () => {
    assert.equal(describeTime(now + 2 * HOUR_MS, now)?.text, "in 2 hours");
  });

  it("is null for nothing and for a value that is not a time, so the caller shows 'unknown'", () => {
    assert.equal(describeTime(null, now), null);
    assert.equal(describeTime(undefined, now), null);
    assert.equal(describeTime("soon", now), null);
    assert.equal(describeTime(1e20, now), null);
  });
});

describe("formatBytes (1 KB = 1024 B, like the design's memory figures)", () => {
  const cases: [number, string][] = [
    [0, "0 B"],
    [1, "1 B"],
    [1023, "1023 B"],
    [1024, "1 KB"],
    [1536, "1.5 KB"],
    [10 * 1024, "10 KB"],
    [10 * 1024 + 300, "10 KB"],
    [10 * 1024 + 700, "11 KB"],
    [1024 * 1024, "1 MB"],
    [5.5 * 1024 * 1024, "5.5 MB"],
    [123 * 1024 * 1024, "123 MB"],
    [1024 ** 3, "1 GB"],
    [5775 * 1024 * 1024, "5.6 GB"],
    [1024 ** 4, "1 TB"],
    [2048 * 1024 ** 4, "2,048 TB"],
  ];
  for (const [bytes, text] of cases) {
    it(`${bytes} is ${text}`, () => {
      assert.equal(formatBytes(bytes), text);
    });
  }

  it("moves up a unit when rounding would print 1024", () => {
    assert.equal(formatBytes(1024 * 1024 - 1), "1 MB");
    assert.equal(formatBytes(1024 ** 3 - 1), "1 GB");
  });

  it("says unknown for a value no size can be", () => {
    for (const bad of [-1, Number.NaN, Number.POSITIVE_INFINITY]) {
      assert.equal(formatBytes(bad), UNKNOWN_TEXT);
    }
  });
});

describe("formatMegabytes", () => {
  it("groups thousands, as the design's memory line does", () => {
    assert.equal(formatMegabytes(5775), "5,775 MB");
    assert.equal(formatMegabytes(8192), "8,192 MB");
    assert.equal(formatMegabytes(64), "64 MB");
    assert.equal(formatMegabytes(0), "0 MB");
  });

  it("says unknown for a negative or non-finite value", () => {
    assert.equal(formatMegabytes(-1), UNKNOWN_TEXT);
    assert.equal(formatMegabytes(Number.NaN), UNKNOWN_TEXT);
  });
});

describe("formatDurationMs", () => {
  const cases: [number, string][] = [
    [0, "0 ms"],
    [840, "840 ms"],
    [999, "999 ms"],
    [999.6, "1 s"],
    [1000, "1 s"],
    [1200, "1.2 s"],
    [9949, "9.9 s"],
    [9950, "10 s"],
    [12_000, "12 s"],
    [59_400, "59 s"],
    [59_600, "1 min"],
    [60_000, "1 min"],
    [185_000, "3 min 5 s"],
    [3_600_000, "1 h"],
    [7_800_000, "2 h 10 min"],
    [86_400_000, "1 d"],
    [100_800_000, "1 d 4 h"],
    [10 * 86_400_000 + 30 * 60_000, "10 d"],
  ];
  for (const [ms, text] of cases) {
    it(`${ms} ms is ${text}`, () => {
      assert.equal(formatDurationMs(ms), text);
    });
  }

  it("says unknown for a negative or non-finite value", () => {
    assert.equal(formatDurationMs(-5), UNKNOWN_TEXT);
    assert.equal(formatDurationMs(Number.NaN), UNKNOWN_TEXT);
  });
});

describe("formatCount", () => {
  it("groups thousands with a comma", () => {
    assert.equal(formatCount(0), "0");
    assert.equal(formatCount(999), "999");
    assert.equal(formatCount(1000), "1,000");
    assert.equal(formatCount(1234567), "1,234,567");
    assert.equal(formatCount(-12345), "-12,345");
  });

  it("rounds a fraction and refuses a non-number", () => {
    assert.equal(formatCount(2.6), "3");
    assert.equal(formatCount(Number.NaN), UNKNOWN_TEXT);
  });
});

describe("formatPercent", () => {
  it("rounds part over whole to a whole percent", () => {
    assert.equal(formatPercent(5775, 7168), "81%");
    assert.equal(formatPercent(0, 7168), "0%");
    assert.equal(formatPercent(7168, 7168), "100%");
    assert.equal(formatPercent(8000, 7168), "112%");
  });

  it("says unknown when there is no whole to compare with", () => {
    assert.equal(formatPercent(1, 0), UNKNOWN_TEXT);
    assert.equal(formatPercent(Number.NaN, 10), UNKNOWN_TEXT);
  });
});

describe("pluralize", () => {
  it("picks the singular for exactly one", () => {
    assert.equal(pluralize(1, "release"), "1 release");
    assert.equal(pluralize(0, "release"), "0 releases");
    assert.equal(pluralize(3, "release"), "3 releases");
    assert.equal(pluralize(1500, "request"), "1,500 requests");
  });

  it("takes an irregular plural", () => {
    assert.equal(pluralize(2, "entry", "entries"), "2 entries");
    assert.equal(pluralize(1, "entry", "entries"), "1 entry");
  });
});
