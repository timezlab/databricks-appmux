/** The resource catalogue as data, and the objects of one type the person can see. */
import {
  ResourceSuggestionList,
  ResourceSuggestionQuery,
  ResourceTypeList,
} from "@/lib/api/schemas";

import { callJson, parseQuery } from "./client";
import type { RequestOptions } from "./client";

export function listResourceTypes(
  options?: RequestOptions,
): Promise<ResourceTypeList> {
  return callJson("GET /resources/types", ResourceTypeList, {
    signal: options?.signal,
  });
}

/**
 * Objects of `query.type` read with the person's token. A Databricks failure is
 * no error: the answer is `200` with `source: "unavailable"` and a `note`, and
 * the form keeps free typing. `async` so that a bad query rejects the promise,
 * like every other refusal of the client.
 */
export async function suggestResources(
  query: ResourceSuggestionQuery,
  options?: RequestOptions,
): Promise<ResourceSuggestionList> {
  return callJson("GET /resources/suggestions", ResourceSuggestionList, {
    query: parseQuery(ResourceSuggestionQuery, query),
    signal: options?.signal,
  });
}
