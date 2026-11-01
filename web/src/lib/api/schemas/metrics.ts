/** GET /metrics. */
import { z } from "./zod.ts";
import { Slug, Time } from "./primitives.ts";

const LATENCY_BUCKETS = 11;

/** admin-api.md, MetricsResponse: `app` is a slug, or `_platform` for the platform row. */
const PLATFORM_ROW = "_platform";

export const MetricsRow = z.object({
  app_slug: z.string(),
  minute: Time,
  requests: z.int(),
  r2xx: z.int(),
  r3xx: z.int(),
  r4xx: z.int(),
  r5xx: z.int(),
  upstream_errors: z.int(),
  limited: z.int(),
  /** Counts per bucket; upper bounds in ms: 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, above. */
  lat_hist: z.array(z.int()).length(LATENCY_BUCKETS),
  bytes_in: z.int(),
  bytes_out: z.int(),
  in_flight_max: z.int(),
  ws_opened: z.int(),
  ws_open: z.int(),
  rss_mb_max: z.int(),
  restarts: z.int(),
  egress_bytes_out: z.int(),
  egress_bytes_in: z.int(),
  egress_dropped: z.int(),
  /** Drops by rule id (`app:2`, `global:1`). */
  egress_dropped_by_rule: z.record(z.string(), z.int()),
  // The last eight are null in an app's row (`_platform` only).
  mem_used_mb: z.int().nullable(),
  mem_limit_mb: z.int().nullable(),
  children_running: z.int().nullable(),
  children_registered: z.int().nullable(),
  admission_rejections: z.int().nullable(),
  registry_errors: z.int().nullable(),
  registry_ms_max: z.int().nullable(),
  edge_rss_mb: z.int().nullable(),
});

export type MetricsRow = z.infer<typeof MetricsRow>;

export const MetricsResponse = z.object({
  from: Time,
  to: Time,
  /** True when the 10000-row cap dropped the oldest minutes (`from` then moved). */
  truncated: z.boolean(),
  items: z.array(MetricsRow),
});

export type MetricsResponse = z.infer<typeof MetricsResponse>;

/**
 * The query string of `GET /metrics`. It is no body of the contract, so it has
 * no example file; `getMetrics` checks it before it builds the URL. The server
 * defaults a missing `to` to now and `from` to one hour before `to`, and
 * answers `400` for a window over 1440 minutes; without `app` a platform admin
 * gets every app and the platform row.
 */
export const MetricsQuery = z.strictObject({
  app: z.union([Slug, z.literal(PLATFORM_ROW)]).optional(),
  from: Time.optional(),
  to: Time.optional(),
});

export type MetricsQuery = z.infer<typeof MetricsQuery>;
