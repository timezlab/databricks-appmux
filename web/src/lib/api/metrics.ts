/** The metrics routes: the rows, and the per-app breakdown. */
import {
  MetricsBreakdown,
  MetricsBreakdownQuery,
  MetricsQuery,
  MetricsResponse,
} from "@/lib/api/schemas";

import { callJson, parseQuery } from "./client";
import type { RequestOptions } from "./client";

/**
 * A window over 7 days or 1440 steps is a `400`. Past 10000 rows the server
 * drops whole oldest steps and answers `truncated: true` with `from` moved.
 * `async` so that a bad query rejects the promise, like every other refusal of
 * the client.
 */
export async function getMetrics(
  query: MetricsQuery = {},
  options?: RequestOptions,
): Promise<MetricsResponse> {
  return callJson("GET /metrics", MetricsResponse, {
    query: parseQuery(MetricsQuery, query),
    signal: options?.signal,
  });
}

/**
 * The top apps by requests in the window, each as a series, and the rest summed
 * as `other`. The window rules are those of `getMetrics`; `limit` is 1 to 20.
 * `async` for the same reason as `getMetrics`.
 */
export async function getMetricsBreakdown(
  query: MetricsBreakdownQuery = {},
  options?: RequestOptions,
): Promise<MetricsBreakdown> {
  return callJson("GET /metrics/breakdown", MetricsBreakdown, {
    query: parseQuery(MetricsBreakdownQuery, query),
    signal: options?.signal,
  });
}
