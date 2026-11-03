/** GET /principals: suggestions for the Sharing form (contracts/admin-api.md, addendum). */
import { z } from "./zod.ts";
import { GrantType } from "./enums.ts";

/** `not_read`: the type was not asked for, or the query was empty. */
const DirectorySource = z.enum([
  "caller_token",
  "platform_token",
  "unavailable",
  "not_read",
]);

/** `name` is what `GrantRequest.name` takes for its `type`. */
export const PrincipalSuggestion = z.object({
  type: GrantType,
  name: z.string(),
  display_name: z.string().nullable(),
  detail: z.string().nullable(),
});

export type PrincipalSuggestion = z.infer<typeof PrincipalSuggestion>;

export const PrincipalList = z.object({
  items: z.array(PrincipalSuggestion),
  truncated: z.boolean(),
  sources: z.object({
    users: DirectorySource,
    groups: DirectorySource,
    service_principals: DirectorySource,
  }),
});

export type PrincipalList = z.infer<typeof PrincipalList>;
