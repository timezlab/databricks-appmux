/** Release routes: update, activate, edit, lock, delete, and the source viewer. */
import {
  ReleaseDetail,
  ReleaseOutcome,
  ReleaseSourceFile,
  ReleaseSourceListing,
} from "@/lib/api/schemas";
import type { UpdateReleaseRequest } from "@/lib/api/schemas";

import { callEmpty, callJson } from "./client";
import type { RequestOptions } from "./client";

/**
 * Starts an Update. `status` tells the two answers apart: `pending` (`202`, a
 * release was created; read `getRelease` for progress) or `up_to_date` (`200`,
 * the folder equals a live release and nothing was created).
 */
export function createRelease(
  app: string,
  options?: RequestOptions,
): Promise<ReleaseOutcome> {
  return callJson("POST /apps/{app}/releases", ReleaseOutcome, {
    params: { app },
    signal: options?.signal,
  });
}

export function getRelease(
  app: string,
  releaseNo: number,
  options?: RequestOptions,
): Promise<ReleaseDetail> {
  return callJson("GET /apps/{app}/releases/{id}", ReleaseDetail, {
    params: { app, id: releaseNo },
    signal: options?.signal,
  });
}

export function activateRelease(
  app: string,
  releaseNo: number,
  options?: RequestOptions,
): Promise<ReleaseDetail> {
  return callJson("POST /apps/{app}/releases/{id}/activate", ReleaseDetail, {
    params: { app, id: releaseNo },
    signal: options?.signal,
  });
}

export function updateRelease(
  app: string,
  releaseNo: number,
  body: UpdateReleaseRequest,
  options?: RequestOptions,
): Promise<ReleaseDetail> {
  return callJson("PUT /apps/{app}/releases/{id}", ReleaseDetail, {
    params: { app, id: releaseNo },
    body,
    signal: options?.signal,
  });
}

export function lockRelease(
  app: string,
  releaseNo: number,
  options?: RequestOptions,
): Promise<ReleaseDetail> {
  return callJson("PUT /apps/{app}/releases/{id}/lock", ReleaseDetail, {
    params: { app, id: releaseNo },
    signal: options?.signal,
  });
}

export function unlockRelease(
  app: string,
  releaseNo: number,
  options?: RequestOptions,
): Promise<ReleaseDetail> {
  return callJson("DELETE /apps/{app}/releases/{id}/lock", ReleaseDetail, {
    params: { app, id: releaseNo },
    signal: options?.signal,
  });
}

export function deleteRelease(
  app: string,
  releaseNo: number,
  options?: RequestOptions,
): Promise<void> {
  return callEmpty("DELETE /apps/{app}/releases/{id}", {
    params: { app, id: releaseNo },
    signal: options?.signal,
  });
}

/** Each call writes an audit row on the server. */
export function listReleaseSource(
  app: string,
  releaseNo: number,
  options?: RequestOptions,
): Promise<ReleaseSourceListing> {
  return callJson(
    "GET /apps/{app}/releases/{id}/source",
    ReleaseSourceListing,
    {
      params: { app, id: releaseNo },
      signal: options?.signal,
    },
  );
}

/** `path` is relative to the snapshot root, with `/` between folders. Each call writes an audit row. */
export function getReleaseSourceFile(
  app: string,
  releaseNo: number,
  path: string,
  options?: RequestOptions,
): Promise<ReleaseSourceFile> {
  return callJson(
    "GET /apps/{app}/releases/{id}/source/{path}",
    ReleaseSourceFile,
    {
      params: { app, id: releaseNo, path },
      signal: options?.signal,
    },
  );
}
