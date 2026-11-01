/** Platform routes: settings, admins, self-check, audit log. */
import {
  AdminList,
  AuditList,
  AuditQuery,
  PlatformSetting,
  PlatformView,
  SelfCheckResult,
} from "@/lib/api/schemas";
import type {
  PutAdminsRequest,
  PutSettingRequest,
  SettingKey,
} from "@/lib/api/schemas";

import { callJson, parseQuery } from "./client";
import type { RequestOptions } from "./client";

export function getPlatform(options?: RequestOptions): Promise<PlatformView> {
  return callJson("GET /platform", PlatformView, { signal: options?.signal });
}

export function putSetting(
  key: SettingKey,
  body: PutSettingRequest,
  options?: RequestOptions,
): Promise<PlatformSetting> {
  return callJson("PUT /platform/settings/{key}", PlatformSetting, {
    params: { key },
    body,
    signal: options?.signal,
  });
}

export function putAdmins(
  body: PutAdminsRequest,
  options?: RequestOptions,
): Promise<AdminList> {
  return callJson("PUT /platform/admins", AdminList, {
    body,
    signal: options?.signal,
  });
}

export function runSelfCheck(
  options?: RequestOptions,
): Promise<SelfCheckResult> {
  return callJson("POST /platform/self-check", SelfCheckResult, {
    signal: options?.signal,
  });
}

// async: a bad query rejects the promise, like every other refusal of the client.
export async function listAudit(
  query: AuditQuery = {},
  options?: RequestOptions,
): Promise<AuditList> {
  return callJson("GET /audit", AuditList, {
    query: parseQuery(AuditQuery, query),
    signal: options?.signal,
  });
}
