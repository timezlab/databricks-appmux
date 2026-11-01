/**
 * Form errors by field, from the issues of a failed `Schema.safeParse(input)`
 * (the request schemas of `lib/api` carry the contract's rules and messages).
 */

/** The key of an error that belongs to the form as a whole (an unknown key, a cross-field rule). */
export const FORM_ERROR_KEY = "_form";

/** The part of a Zod issue this needs; `error.issues` satisfies it. */
export interface FieldIssue {
  readonly path: readonly PropertyKey[];
  readonly message: string;
}

/** One message per field, keyed by its dotted path (`slug`, `rules.2.cidr`): the first issue of each. */
export function fieldErrorsFromIssues(
  issues: readonly FieldIssue[],
): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const issue of issues) {
    const key =
      issue.path.length === 0
        ? FORM_ERROR_KEY
        : issue.path.map(String).join(".");
    errors[key] ??= issue.message;
  }
  return errors;
}
