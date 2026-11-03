/** Metrics for the dashboard tests: rows parsed against the real schema, so a test row is a legal one. */
import assert from "node:assert/strict";

import { MetricsResponse, MetricsRow } from "@/lib/api/schemas";
import { buildDashboardModel } from "@/lib/dashboard/model";
import type { ChartSpec, SeriesModel } from "@/lib/dashboard/model";
import type { DashboardScope } from "@/lib/dashboard/scope";
import { MINUTE_MS } from "@/lib/dashboard/series";
import {
  readExample,
  readExampleObject,
} from "../../support/contract-examples";

export const WINDOW_START_MS = Date.parse("2026-10-30T08:00:00Z");
export const WINDOW_MINUTES = 30;

export function isoAt(offsetMinutes: number): string {
  return new Date(WINDOW_START_MS + offsetMinutes * MINUTE_MS)
    .toISOString()
    .replace(".000Z", "Z");
}

/** The contract's example answer: one minute of `sales` and one of the platform. */
export function contractMetrics(): MetricsResponse {
  return MetricsResponse.parse(readExample("MetricsResponse"));
}

/** A platform row `offsetMinutes` after the start of the window. */
export function platformRow(
  offsetMinutes: number,
  overrides: Record<string, unknown> = {},
): MetricsRow {
  return MetricsRow.parse({
    ...readExampleObject("MetricsRow"),
    app_slug: "_platform",
    minute: isoAt(offsetMinutes),
    requests: 100,
    r4xx: 10,
    r5xx: 0,
    mem_used_mb: 5000 + offsetMinutes * 10,
    mem_limit_mb: 7168,
    children_running: 3,
    children_registered: 4,
    egress_dropped: 0,
    ...overrides,
  });
}

export function metricsResponse(items: MetricsRow[]): MetricsResponse {
  return MetricsResponse.parse({
    from: isoAt(0),
    to: isoAt(WINDOW_MINUTES),
    truncated: false,
    items,
  });
}

/**
 * Half an hour of the platform with a hole in it: minutes 10 to 14 were never
 * reported, minute 3 had two server errors, minute 7 dropped four packets, and
 * minute 20 was reported with no traffic at all.
 */
export const GAP_FIRST_MINUTE = 10;
export const GAP_LAST_MINUTE = 14;
export const ERROR_MINUTE = 3;
export const IDLE_MINUTE = 20;
export const DROP_MINUTE = 7;

export function platformHalfHour(): MetricsResponse {
  const items: MetricsRow[] = [];
  for (let minute = 0; minute < WINDOW_MINUTES; minute++) {
    if (minute >= GAP_FIRST_MINUTE && minute <= GAP_LAST_MINUTE) continue;
    items.push(
      platformRow(minute, {
        ...(minute === ERROR_MINUTE ? { r5xx: 2 } : {}),
        ...(minute === DROP_MINUTE ? { egress_dropped: 4 } : {}),
        ...(minute === IDLE_MINUTE
          ? { requests: 0, r4xx: 0, lat_hist: new Array<number>(11).fill(0) }
          : {}),
      }),
    );
  }
  return metricsResponse(items);
}

export const PLATFORM: DashboardScope = { kind: "platform" };

export function seriesModel(
  response: MetricsResponse,
  scope: DashboardScope = PLATFORM,
): SeriesModel {
  const model = buildDashboardModel(response, scope);
  assert.ok(
    model.kind === "series",
    `expected a series model, got ${model.kind}`,
  );
  return model;
}

export function chartSpec(model: SeriesModel, id: ChartSpec["id"]): ChartSpec {
  const spec = model.charts.find((candidate) => candidate.id === id);
  assert.ok(spec !== undefined, `no ${id} chart`);
  return spec;
}
