/** GET /resources/types and GET /resources/suggestions: the catalogue as data, and what exists. */
import { z } from "./zod.ts";
import { AccessAs } from "./enums.ts";

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
