/** Permission requests and their decisions. */
import { RequestList, RequestRow } from "@/lib/api/schemas";
import type {
  CreatePermissionRequest,
  DecidePermissionRequest,
} from "@/lib/api/schemas";

import { callJson } from "./client";
import type { RequestOptions } from "./client";

export function listRequests(options?: RequestOptions): Promise<RequestList> {
  return callJson("GET /requests", RequestList, { signal: options?.signal });
}

export function createRequest(
  body: CreatePermissionRequest,
  options?: RequestOptions,
): Promise<RequestRow> {
  return callJson("POST /requests", RequestRow, {
    body,
    signal: options?.signal,
  });
}

export function withdrawRequest(
  requestId: number,
  options?: RequestOptions,
): Promise<RequestRow> {
  return callJson("POST /requests/{id}/withdraw", RequestRow, {
    params: { id: requestId },
    signal: options?.signal,
  });
}

export function approveRequest(
  requestId: number,
  body: DecidePermissionRequest = {},
  options?: RequestOptions,
): Promise<RequestRow> {
  return callJson("POST /requests/{id}/approve", RequestRow, {
    params: { id: requestId },
    body,
    signal: options?.signal,
  });
}

export function denyRequest(
  requestId: number,
  body: DecidePermissionRequest = {},
  options?: RequestOptions,
): Promise<RequestRow> {
  return callJson("POST /requests/{id}/deny", RequestRow, {
    params: { id: requestId },
    body,
    signal: options?.signal,
  });
}
