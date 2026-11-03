/** GET /resources/types and GET /resources/suggestions: the catalogue as data, and what exists. */
import { z } from "./zod.ts";
import { AccessAs } from "./enums.ts";
import { NotBlank, plainText } from "./primitives.ts";

/** admin-api.md addendum, GET /resources/suggestions: `q` is at most 256 characters, `limit` 1 to 50. */
const QUERY_MAX_LENGTH = 256;
const LIMIT_MAX = 50;

export const ResourceTypeEntry = z.object({
  type: z.string(),
  label: z.string(),
  description: z.string(),
  /** The levels the type allows; the first is the default. */
  permissions: z.array(z.string()),
  /** Empty for a `secret`, which has no `access_as`. */
  access_as: z.array(AccessAs),
  /** An example target, for the field's placeholder. */
  target_hint: z.string(),
  can_suggest: z.boolean(),
});

export type ResourceTypeEntry = z.infer<typeof ResourceTypeEntry>;

export const ResourceTypeList = z.object({
  items: z.array(ResourceTypeEntry),
});

export type ResourceTypeList = z.infer<typeof ResourceTypeList>;

export const ResourceSuggestion = z.object({
  /** For a secret scope: `<scope>/`, a prefix to continue typing and not a complete target. */
  target: z.string(),
  label: z.string(),
  detail: z.string().nullable(),
  permissions: z.array(z.string()),
});

export type ResourceSuggestion = z.infer<typeof ResourceSuggestion>;

export const ResourceSuggestionList = z.object({
  items: z.array(ResourceSuggestion),
  truncated: z.boolean(),
  source: z.enum(["caller_token", "unavailable", "not_supported"]),
  /** In words, why the list is empty or short; null when there is nothing to say. */
  note: z.string().nullable(),
});

export type ResourceSuggestionList = z.infer<typeof ResourceSuggestionList>;

/**
 * The query string of `GET /resources/suggestions`. It is no body of the
 * contract, so it has no example file; `suggestResources` checks it before it
 * builds the URL. `type` is a name from `GET /resources/types`: the catalogue
 * is data, so the client holds no list of the names and the server answers
 * `400` `unknown_type` for one it does not know. The server defaults `q` to
 * nothing and `limit` to 20.
 */
export const ResourceSuggestionQuery = z.strictObject({
  type: NotBlank,
  q: plainText(QUERY_MAX_LENGTH).optional(),
  limit: z.int().min(1).max(LIMIT_MAX).optional(),
});

export type ResourceSuggestionQuery = z.infer<typeof ResourceSuggestionQuery>;
