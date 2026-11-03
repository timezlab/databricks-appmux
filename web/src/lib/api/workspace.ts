/** The workspace folder browser behind the source-folder field. */
import { WorkspaceBrowseQuery, WorkspaceListing } from "@/lib/api/schemas";

import { callJson, parseQuery } from "./client";
import type { RequestOptions } from "./client";

/**
 * One folder of the person's workspace, read with their token; no `path` lists
 * the roots. A path the server does not accept (not under `/Workspace/`, an
 * empty, `.` or `..` segment) comes back as an `ApiError` with the server's
 * code, not as `INVALID_REQUEST`. `async` so that a bad query rejects the
 * promise, like every other refusal of the client.
 */
export async function browseWorkspace(
  query: WorkspaceBrowseQuery = {},
  options?: RequestOptions,
): Promise<WorkspaceListing> {
  return callJson("GET /workspace/browse", WorkspaceListing, {
    query: parseQuery(WorkspaceBrowseQuery, query),
    signal: options?.signal,
  });
}
