/** Who may open and manage an app. */
import { AppGrant } from "@/lib/api/schemas";
import type { GrantRequest, GrantType } from "@/lib/api/schemas";

import { callEmpty, callJson } from "./client";
import type { RequestOptions } from "./client";

export function putGrant(
  app: string,
  body: GrantRequest,
  options?: RequestOptions,
): Promise<AppGrant> {
  return callJson("PUT /apps/{app}/grants", AppGrant, {
    params: { app },
    body,
    signal: options?.signal,
  });
}

export function deleteGrant(
  app: string,
  type: GrantType,
  name: string,
  options?: RequestOptions,
): Promise<void> {
  return callEmpty("DELETE /apps/{app}/grants/{type}/{name}", {
    params: { app, type, name },
    signal: options?.signal,
  });
}
