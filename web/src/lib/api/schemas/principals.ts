/** GET /principals: suggestions for the Sharing form (contracts/admin-api.md, addendum). */
import { z } from "./zod.ts";
import { GrantType } from "./enums.ts";

/** admin-api.md addendum, GET /principals: the limits of the query string. */
const QUERY_MAX_LENGTH = 64;
const LIMIT_MAX = 50;

/** The characters the server lets into the text it puts in a SCIM filter; anything else is a `400`. */
const QUERY_PATTERN = /^[A-Za-z0-9 ._@+-]*$/;

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

/**
 * The query string of `GET /principals`. It is no body of the contract, so it
 * has no example file; `searchPrincipals` checks it before it builds the URL
 * and sends `types` as a comma list. The server defaults a missing `q` to
 * nothing (then only the `everyone` row comes back), `types` to all four and
 * `limit` to 10.
 */
export const PrincipalQuery = z.strictObject({
  q: z
    .string()
    .max(QUERY_MAX_LENGTH)
    .regex(
      QUERY_PATTERN,
      "letters, digits, space and . _ @ + - only, at most 64 characters",
    )
    .optional(),
  types: z.array(GrantType).min(1).optional(),
  limit: z.int().min(1).max(LIMIT_MAX).optional(),
});

export type PrincipalQuery = z.infer<typeof PrincipalQuery>;
