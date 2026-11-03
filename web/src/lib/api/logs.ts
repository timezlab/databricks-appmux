/** The output of an app's child. */
import { AppLogs, AppLogsQuery } from "@/lib/api/schemas";

import { callJson, parseQuery } from "./client";
import type { RequestOptions } from "./client";

/**
 * The newest `limit` lines after `after`; poll with `after` set to the last
 * answer's `next_after`, and clear the view when `instance` changes. `async` so
 * that a bad query rejects the promise, like every other refusal of the client.
 */
export async function getAppLogs(
  app: string,
  query: AppLogsQuery = {},
  options?: RequestOptions,
): Promise<AppLogs> {
  return callJson("GET /apps/{app}/logs", AppLogs, {
    params: { app },
    query: parseQuery(AppLogsQuery, query),
    signal: options?.signal,
  });
}
