/** GET /metrics/breakdown. */
import { z } from "./zod.ts";
import { MetricStep, MetricsRow } from "./metrics.ts";
import { Time } from "./primitives.ts";

/** admin-api.md addendum, GET /metrics/breakdown: `limit` is 1 to 20. */
const BREAKDOWN_LIMIT_MAX = 20;

export { MetricStep };

/** One step of a series: `minute` is its start; the columns are MetricsRow's. */
export const MetricsPoint = MetricsRow.pick({
  minute: true,
  requests: true,
  r4xx: true,
  r5xx: true,
  rss_mb_max: true,
  bytes_out: true,
  egress_dropped: true,
  lat_hist: true,
});

export type MetricsPoint = z.infer<typeof MetricsPoint>;

export const MetricsSeries = z.object({
  app_slug: z.string(),
  /** Null for an app that is no longer registered. */
  display_name: z.string().nullable(),
  /** Oldest step first; a step with no minute is left out, not filled. */
  points: z.array(MetricsPoint),
});

export type MetricsSeries = z.infer<typeof MetricsSeries>;

export const MetricsBreakdown = z.object({
  from: Time,
  to: Time,
  step: MetricStep,
  /** The top apps by requests in the window, most first. */
  series: z.array(MetricsSeries),
  /**
   * The apps below the cut added up step by step, every column summed (so its
   * `rss_mb_max` is the sum of those apps' peaks); null when every app has a
   * series.
   */
  other: z.array(MetricsPoint).nullable(),
});

export type MetricsBreakdown = z.infer<typeof MetricsBreakdown>;

/**
 * The query string of `GET /metrics/breakdown`. It is no body of the contract,
 * so it has no example file. The server defaults `to` to now, `from` to one
 * hour before `to`, `step` to `1m` and `limit` to 8, and applies the window
 * rule of `GET /metrics`.
 */
export const MetricsBreakdownQuery = z.strictObject({
  from: Time.optional(),
  to: Time.optional(),
  step: MetricStep.optional(),
  limit: z.int().min(1).max(BREAKDOWN_LIMIT_MAX).optional(),
});

export type MetricsBreakdownQuery = z.infer<typeof MetricsBreakdownQuery>;
