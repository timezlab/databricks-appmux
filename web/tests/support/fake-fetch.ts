/** A stand-in for `fetch` that the client tests share: it records each request and answers as the test says. */
import { vi } from "vitest";

export interface RecordedCall {
  url: string;
  init: RequestInit;
}

export function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/**
 * Replaces `globalThis.fetch` until `vi.restoreAllMocks()`. `handle` sees every
 * call and returns the response; a test keeps its own `calls` and `respond`
 * variables and reassigns them, so `handle` reads them when the call happens.
 */
export function stubFetch(
  handle: (call: RecordedCall) => Response | Promise<Response>,
): void {
  vi.spyOn(globalThis, "fetch").mockImplementation(((
    url: string,
    init: RequestInit,
  ) => Promise.resolve(handle({ url, init }))) as typeof fetch);
}
