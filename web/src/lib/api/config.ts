/** Configuration and resource routes. */
import {
  ConfigChangeResult,
  RequirementsResponse,
  ResourceView,
} from "@/lib/api/schemas";
import type {
  AttachResourceRequest,
  UpdateConfigRequest,
} from "@/lib/api/schemas";

import { callEmpty, callJson } from "./client";
import type { RequestOptions } from "./client";

export function updateConfig(
  app: string,
  body: UpdateConfigRequest,
  options?: RequestOptions,
): Promise<ConfigChangeResult> {
  return callJson("PUT /apps/{app}/config", ConfigChangeResult, {
    params: { app },
    body,
    signal: options?.signal,
  });
}

export function getRequirements(
  app: string,
  options?: RequestOptions,
): Promise<RequirementsResponse> {
  return callJson("GET /apps/{app}/requirements", RequirementsResponse, {
    params: { app },
    signal: options?.signal,
  });
}

export function attachResource(
  app: string,
  key: string,
  body: AttachResourceRequest,
  options?: RequestOptions,
): Promise<ResourceView> {
  return callJson("PUT /apps/{app}/resources/{key}", ResourceView, {
    params: { app, key },
    body,
    signal: options?.signal,
  });
}

export function detachResource(
  app: string,
  key: string,
  options?: RequestOptions,
): Promise<void> {
  return callEmpty("DELETE /apps/{app}/resources/{key}", {
    params: { app, key },
    signal: options?.signal,
  });
}
