/** What an app's child is started with. */
import { AppEnvironment } from "@/lib/api/schemas";

import { callJson } from "./client";
import type { RequestOptions } from "./client";

export function getAppEnvironment(
  app: string,
  options?: RequestOptions,
): Promise<AppEnvironment> {
  return callJson("GET /apps/{app}/environment", AppEnvironment, {
    params: { app },
    signal: options?.signal,
  });
}
