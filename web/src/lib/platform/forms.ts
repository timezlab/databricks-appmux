/**
 * What every form of the platform page shares: the shape of a parse result,
 * a strict whole-number reader, and the wording of a failed schema check.
 *
 * The forms build the request bodies of lib/api/schemas and let those schemas
 * decide (they carry the contract's limits), so a form cannot accept a body the
 * server answers 400 for. Zod's default English ("Too small: expected number to
 * be >=64") is rewritten here into short field messages; the contract's own
 * messages (`must start with /Workspace/`, `an allow cannot overlap
 * @container_ip`) pass through unchanged.
 */
import type { z } from "zod";

import {
  FORM_ERROR_KEY,
  fieldErrorsFromIssues,
} from "@/lib/forms/field-errors";
import { formatCount, pluralize } from "@/lib/format";

/** One message per field (dotted path), or under `_form` for the form as a whole. */
export type FormErrors = Readonly<Record<string, string>>;

/** A form either yields the body to send, or the message of each field to fix. */
export type FormParse<T> =
  { ok: true; value: T } | { ok: false; errors: FormErrors };

export { FORM_ERROR_KEY };

type ZodIssue = z.core.$ZodIssue;

// Past 15 digits a JavaScript number no longer holds the value that was typed.
const MAX_WHOLE_NUMBER_DIGITS = 15;
const WHOLE_NUMBER = new RegExp(`^\\d{1,${MAX_WHOLE_NUMBER_DIGITS}}$`);

/**
 * Plain digits only. `Number("1e3")`, `Number("0x10")` and `Number("")` all
 * succeed in JavaScript and would turn a typo into a different budget.
 */
export function parseWholeNumber(text: string): number | null {
  const trimmed = text.trim();
  return WHOLE_NUMBER.test(trimmed) ? Number(trimmed) : null;
}

/** The short message for one issue. */
export function issueMessage(issue: ZodIssue): string {
  if (issue.code === "too_small" || issue.code === "too_big") {
    const bound = Number(
      issue.code === "too_small" ? issue.minimum : issue.maximum,
    );
    const direction = issue.code === "too_small" ? "at least" : "at most";
    if (issue.origin === "string") {
      return `must be ${direction} ${pluralize(bound, "character")}`;
    }
    if (issue.origin === "array") {
      return `must have ${direction} ${pluralize(bound, "item")}`;
    }
    return `must be ${direction} ${formatCount(bound)}`;
  }
  if (issue.code === "invalid_type") {
    return issue.expected === "number" || issue.expected === "int"
      ? "must be a number"
      : "is required";
  }
  return issue.message;
}

/** `error.issues` of a failed `safeParse`, as one friendly message per field. */
export function formErrors(issues: readonly ZodIssue[]): FormErrors {
  return fieldErrorsFromIssues(
    issues.map((issue) => ({
      path: issue.path,
      message: issueMessage(issue),
    })),
  );
}

/** A form failure with a single message about the whole form. */
export function formFailure(message: string): {
  ok: false;
  errors: FormErrors;
} {
  return { ok: false, errors: { [FORM_ERROR_KEY]: message } };
}
