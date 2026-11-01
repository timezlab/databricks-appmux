/** The metrics route. */
import { MetricsQuery, MetricsResponse } from "@/lib/api/schemas";

import { callJson, parseQuery } from "./client";
import type { RequestOptions } from "./client";

/**
 * A window over 1440 minutes is a `400`. Past 10000 rows the server drops whole
 * oldest minutes and answers `truncated: true` with `from` moved forward.
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
