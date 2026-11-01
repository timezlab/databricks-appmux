/**
 * Boundary tests of the limits and rules that the request schemas enforce.
 * Every number below is written as a literal from admin-api.md on purpose: a
 * test that reads the schema's own constant would follow a mutation of it.
 */
import assert from "node:assert/strict";
import { describe, it } from "vitest";

import * as api from "@/lib/api";
import {
  readExampleObject,
  without,
} from "../../../../support/contract-examples";
import {
  accepts,
  rejects,
  variant,
} from "../../../../support/schema-assertions";

describe("metrics", () => {
  const row = readExampleObject("MetricsRow");

  it("lat_hist holds exactly 11 counts", () => {
    const histogram = (length: number) => Array.from({ length }, () => 0);
    accepts(api.MetricsRow, variant(row, { lat_hist: histogram(11) }));
    rejects(api.MetricsRow, variant(row, { lat_hist: histogram(10) }));
    rejects(api.MetricsRow, variant(row, { lat_hist: histogram(12) }));
  });

  it("the response says whether the row cap dropped minutes", () => {
    const response = readExampleObject("MetricsResponse");
    assert.equal(response.truncated, false);
    accepts(api.MetricsResponse, variant(response, { truncated: true }));
    rejects(api.MetricsResponse, variant(response, { truncated: "yes" }));
    rejects(
      api.MetricsResponse,
      without(response, "truncated"),
      "truncated is always present",
    );
  });
});

describe("MetricsQuery (GET /metrics?app=&from=&to=)", () => {
  it("takes every parameter as optional; app is a slug or _platform", () => {
    accepts(api.MetricsQuery, {});
    accepts(api.MetricsQuery, {
      app: "sales",
      from: "2026-10-30T08:00:00Z",
      to: "2026-10-30T09:00:00.000Z",
    });
    accepts(api.MetricsQuery, { app: "_platform" });
    for (const bad of ["Sales", "a/b", "", "_other"]) {
      rejects(api.MetricsQuery, { app: bad }, bad);
    }
  });

  it("wants ISO 8601 UTC times", () => {
    for (const bad of [
      "yesterday",
      "2026-10-30",
      "2026-10-30T08:00:00+02:00",
    ]) {
      rejects(api.MetricsQuery, { from: bad }, bad);
      rejects(api.MetricsQuery, { to: bad }, bad);
    }
  });

  it("refuses a parameter the route does not have, limit included", () => {
    rejects(api.MetricsQuery, { limit: 10 });
  });
});
