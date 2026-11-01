/** Identity and launcher routes. */
import { AppList, MeResponse } from "@/lib/api/schemas";
import type { OpenModeRequest } from "@/lib/api/schemas";

import { callEmpty, callJson } from "./client";
import type { RequestOptions } from "./client";

export function getMe(options?: RequestOptions): Promise<MeResponse> {
  return callJson("GET /me", MeResponse, { signal: options?.signal });
}

export function listApps(options?: RequestOptions): Promise<AppList> {
  return callJson("GET /apps", AppList, { signal: options?.signal });
}

export function setOpenMode(
  app: string,
  body: OpenModeRequest,
  options?: RequestOptions,
): Promise<void> {
  return callEmpty("POST /apps/{app}/open-mode", {
    params: { app },
    body,
    signal: options?.signal,
  });
}
