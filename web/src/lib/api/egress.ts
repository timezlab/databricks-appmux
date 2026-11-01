/** Egress rules, platform-wide and per app. */
import { AppDetail, EgressRules } from "@/lib/api/schemas";
import type { PutAppEgressRequest, PutEgressRequest } from "@/lib/api/schemas";

import { callJson } from "./client";
import type { RequestOptions } from "./client";

export function putPlatformEgress(
  body: PutEgressRequest,
  options?: RequestOptions,
): Promise<EgressRules> {
  return callJson("PUT /platform/egress", EgressRules, {
    body,
    signal: options?.signal,
  });
}

export function putAppEgress(
  app: string,
  body: PutAppEgressRequest,
  options?: RequestOptions,
): Promise<AppDetail> {
  return callJson("PUT /apps/{app}/egress", AppDetail, {
    params: { app },
    body,
    signal: options?.signal,
  });
}
