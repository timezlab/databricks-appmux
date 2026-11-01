/** App routes: register, edit, owner, delete, start, stop, restart. */
import { AppDetail, AppStateResponse } from "@/lib/api/schemas";
import type {
  RegisterAppRequest,
  SetOwnerRequest,
  UpdateAppRequest,
} from "@/lib/api/schemas";

import { callEmpty, callJson } from "./client";
import type { RequestOptions } from "./client";

export function registerApp(
  body: RegisterAppRequest,
  options?: RequestOptions,
): Promise<AppDetail> {
  return callJson("POST /apps", AppDetail, { body, signal: options?.signal });
}

export function getApp(
  app: string,
  options?: RequestOptions,
): Promise<AppDetail> {
  return callJson("GET /apps/{app}", AppDetail, {
    params: { app },
    signal: options?.signal,
  });
}

export function updateApp(
  app: string,
  body: UpdateAppRequest,
  options?: RequestOptions,
): Promise<AppDetail> {
  return callJson("PATCH /apps/{app}", AppDetail, {
    params: { app },
    body,
    signal: options?.signal,
  });
}

export function setOwner(
  app: string,
  body: SetOwnerRequest,
  options?: RequestOptions,
): Promise<AppDetail> {
  return callJson("PUT /apps/{app}/owner", AppDetail, {
    params: { app },
    body,
    signal: options?.signal,
  });
}

export function deleteApp(
  app: string,
  options?: RequestOptions,
): Promise<void> {
  return callEmpty("DELETE /apps/{app}", {
    params: { app },
    signal: options?.signal,
  });
}

export function startApp(
  app: string,
  options?: RequestOptions,
): Promise<AppStateResponse> {
  return callJson("POST /apps/{app}/start", AppStateResponse, {
    params: { app },
    signal: options?.signal,
  });
}

export function stopApp(
  app: string,
  options?: RequestOptions,
): Promise<AppStateResponse> {
  return callJson("POST /apps/{app}/stop", AppStateResponse, {
    params: { app },
    signal: options?.signal,
  });
}

export function restartApp(
  app: string,
  options?: RequestOptions,
): Promise<AppStateResponse> {
  return callJson("POST /apps/{app}/restart", AppStateResponse, {
    params: { app },
    signal: options?.signal,
  });
}
