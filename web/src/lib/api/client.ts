/**
 * The transport every route function shares: the only file that calls `fetch`.
 *
 * Requests are same-origin only (`mode: "same-origin"`, `credentials:
 * "same-origin"`): the browser then sends `Sec-Fetch-Site: same-origin`, which
 * the edge requires for every state-changing call, and a cross-origin target
 * fails instead of leaving the admin origin. The caller's identity is the
 * forwarded one of the edge; the client holds no token.
 */
import type { z } from "zod";

import { ErrorBody } from "@/lib/api/schemas";

import {
  ApiError,
  INVALID_REQUEST,
  INVALID_RESPONSE,
  NETWORK_ERROR,
  httpStatusCode,
} from "./errors";
import { expandRoute, withQuery } from "./routes";
import type { ApiRoute, PathValue, RouteQuery } from "./routes";

const MAX_REPORTED_SCHEMA_ISSUES = 3;

/** What a caller may add to any route function, as its last argument. */
export interface RequestOptions {
  /**
   * Cancels the request. An abort comes out of the route function as it came
   * out of `fetch` (an `AbortError`), not as an `ApiError`: nobody is waiting
   * for the answer, so it is no failure to show.
   */
  signal?: AbortSignal;
}

interface CallOptions extends RequestOptions {
  params?: Record<string, PathValue>;
  query?: RouteQuery;
  body?: unknown;
}

/** The caller gave up (`signal`), or the browser did (navigation): `cause` is no failure of the API. */
function isAbort(cause: unknown, signal: AbortSignal | undefined): boolean {
  return (
    signal?.aborted === true ||
    (cause instanceof Error && cause.name === "AbortError")
  );
}

async function readErrorBody(
  response: Response,
  signal: AbortSignal | undefined,
): Promise<ApiError> {
  try {
    const parsed = ErrorBody.safeParse(await response.json());
    if (parsed.success) return ApiError.fromBody(response.status, parsed.data);
  } catch (cause) {
    if (isAbort(cause, signal)) throw cause;
    // The body was not JSON (a proxy page, for example); the status-based
    // error below says what the person can do, without echoing that body.
  }
  return new ApiError(
    response.status,
    httpStatusCode(response.status),
    `The server answered ${response.status} without an error message; retry, and tell a platform admin if it persists.`,
  );
}

async function send(route: ApiRoute, options: CallOptions): Promise<Response> {
  const { method, url } = expandRoute(route, options.params ?? {});
  const headers: Record<string, string> = { Accept: "application/json" };
  const init: RequestInit = {
    method,
    headers,
    credentials: "same-origin",
    mode: "same-origin",
    cache: "no-store",
  };
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  if (options.signal !== undefined) init.signal = options.signal;

  let response: Response;
  try {
    response = await fetch(withQuery(url, options.query), init);
  } catch (cause) {
    if (isAbort(cause, options.signal)) throw cause;
    throw new ApiError(
      0,
      NETWORK_ERROR,
      "Could not reach the appmux API; check the connection and retry.",
      {
        cause,
      },
    );
  }
  if (!response.ok) throw await readErrorBody(response, options.signal);
  return response;
}

function describeIssues(error: z.ZodError): string {
  return error.issues
    .slice(0, MAX_REPORTED_SCHEMA_ISSUES)
    .map((issue) => `${issue.path.join(".") || "body"}: ${issue.message}`)
    .join("; ");
}

/**
 * Checks a query against its schema before a URL is built, so a bad value
 * never leaves the browser. Status 0, as for a call that got no answer: there
 * is none to report, and `message` says what to fix.
 */
export function parseQuery<S extends z.ZodType>(
  schema: S,
  query: unknown,
): z.output<S> {
  const parsed = schema.safeParse(query);
  if (!parsed.success) {
    throw new ApiError(
      0,
      INVALID_REQUEST,
      `The query is not valid (${describeIssues(parsed.error)}); this is a bug in the page.`,
      { cause: parsed.error },
    );
  }
  return parsed.data;
}

export async function callJson<S extends z.ZodType>(
  route: ApiRoute,
  schema: S,
  options: CallOptions = {},
): Promise<z.output<S>> {
  const response = await send(route, options);
  let body: unknown;
  try {
    body = await response.json();
  } catch (cause) {
    if (isAbort(cause, options.signal)) throw cause;
    throw new ApiError(
      response.status,
      INVALID_RESPONSE,
      "The server answered with something that is not JSON.",
      {
        cause,
      },
    );
  }
  const parsed = schema.safeParse(body);
  if (!parsed.success) {
    throw new ApiError(
      response.status,
      INVALID_RESPONSE,
      `The server answered with an unexpected body (${describeIssues(parsed.error)}); reload, and tell a platform admin if it persists.`,
      { cause: parsed.error },
    );
  }
  return parsed.data;
}

export async function callEmpty(
  route: ApiRoute,
  options: CallOptions = {},
): Promise<void> {
  await send(route, options);
}
