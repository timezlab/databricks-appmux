/** The principal search that feeds the Sharing form. */
import { PrincipalList, PrincipalQuery } from "@/lib/api/schemas";

import { callJson, parseQuery } from "./client";
import type { RequestOptions } from "./client";

/**
 * Suggestions for a grant: `everyone`, groups, users and service principals
 * whose name starts the text in `q`. `types` goes out as one comma list. An
 * empty or missing `q` reads no directory and returns only the `everyone` row.
 * `async` so that a bad query rejects the promise, like every other refusal of
 * the client.
 */
export async function searchPrincipals(
  query: PrincipalQuery = {},
  options?: RequestOptions,
): Promise<PrincipalList> {
  const { q, types, limit } = parseQuery(PrincipalQuery, query);
  return callJson("GET /principals", PrincipalList, {
    query: { q, types: types?.join(","), limit },
    signal: options?.signal,
  });
}
