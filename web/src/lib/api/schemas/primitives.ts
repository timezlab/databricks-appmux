/** Building blocks shared by every schema family: the contract's patterns, limits and field types. */
import { z } from "./zod.ts";

// ---------------------------------------------------------------- primitives

/** ISO 8601 UTC, e.g. `2026-10-30T09:14:00Z`; kept as the wire string. */
export const Time = z.iso.datetime();

/** admin-api.md, RegisterAppRequest.slug. `$` without the `m` flag matches only at the very end, so `"sales\n"` fails. */
export const SLUG_PATTERN = /^[a-z0-9][a-z0-9-]{0,30}$/;

const RESOURCE_KEY_PATTERN = /^[a-z0-9][a-z0-9._-]{0,62}$/;

const WORKSPACE_PATH_PREFIX = "/Workspace/";

export const MEMORY_BUDGET_MIN_MB = 64;

export const MEMORY_BUDGET_MAX_MB = 16384;

/** Counted in UTF-16 units like the schema, which only ever refuses sooner than the server. */
export const REASON_MAX_LENGTH = 500;

export const Slug = z
  .string()
  .regex(
    SLUG_PATTERN,
    "lower-case letters, digits and -, at most 31 characters",
  );

/** The key of an app resource: the path parameter of attach and detach, a `value_from`, an own-SP key. */
export const ResourceKey = z
  .string()
  .regex(
    RESOURCE_KEY_PATTERN,
    "lower-case letters, digits, '.', '_' and '-', starting with a letter or digit, at most 63 characters",
  );

/**
 * The shape the server accepts (`appmux.settings.normalise_email`): a name,
 * `@`, and a domain of at least two dot-separated labels; no space, comma or
 * semicolon anywhere, so a pasted list is not one address. `z.email()` is
 * stricter than that (it refuses `a#b@example.com`), so a person with such an
 * address could not be named; the shape is spelled out instead.
 */
const EMAIL_SHAPE_PATTERN = /^[^@\s,;]+@[^@\s,;.]+(?:\.[^@\s,;.]+)+$/;

export const LowerCaseEmail = z
  .string()
  .regex(EMAIL_SHAPE_PATTERN, "must be an email address (name@domain)")
  .refine((value) => value === value.toLowerCase(), "must be lower case");

export const WorkspacePath = z
  .string()
  .startsWith(WORKSPACE_PATH_PREFIX, "must start with /Workspace/");

export const MemoryBudgetMb = z
  .int()
  .min(MEMORY_BUDGET_MIN_MB)
  .max(MEMORY_BUDGET_MAX_MB);

export const NotBlank = z
  .string()
  .refine((value) => value.trim().length > 0, "must not be blank");

export const Reason = NotBlank.max(REASON_MAX_LENGTH);

/** Unicode category `Cc`, which is what the server's `unicodedata.category(character) == "Cc"` calls a control character. */
const CONTROL_CHARACTER_PATTERN = /\p{Cc}/u;

/**
 * Text of at most `maxLength` characters with no control character, for the
 * query values the server refuses on those two counts (`q` of the resource
 * suggestions, `path` of the workspace browser). Characters are code points, as
 * the server counts them, so an emoji is one and not two.
 */
export function plainText(maxLength: number) {
  return z
    .string()
    .refine(
      (text) => Array.from(text).length <= maxLength,
      `at most ${maxLength} characters`,
    )
    .refine(
      (text) => !CONTROL_CHARACTER_PATTERN.test(text),
      "must not hold a control character",
    );
}

export const PLATFORM_FIELDS = {
  slug: Slug,
  display_name: NotBlank,
  source_path: WorkspacePath,
  memory_budget_mb: MemoryBudgetMb,
};
