/** Delegation (OBO, shared SP) of an app. */
import { AppDetail } from "@/lib/api/schemas";
import type {
  DelegationKind,
  DelegationReasonRequest,
} from "@/lib/api/schemas";

import { callJson } from "./client";
import type { RequestOptions } from "./client";

export function grantDelegation(
  app: string,
  kind: DelegationKind,
  body: DelegationReasonRequest,
  options?: RequestOptions,
): Promise<AppDetail> {
  return callJson("PUT /apps/{app}/delegation/{kind}", AppDetail, {
    params: { app, kind },
    body,
    signal: options?.signal,
  });
}

/**
 * The DELETE has no body (nothing shows a DELETE body survives the Databricks
 * proxy): the reason it is recorded with and the `apps` version travel as query
 * parameters.
 */
export function revokeDelegation(
  app: string,
  kind: DelegationKind,
  { reason, version }: DelegationReasonRequest,
  options?: RequestOptions,
): Promise<AppDetail> {
  return callJson("DELETE /apps/{app}/delegation/{kind}", AppDetail, {
    params: { app, kind },
    query: { reason, version },
    signal: options?.signal,
  });
}
