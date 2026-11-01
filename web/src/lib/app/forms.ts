/**
 * Shared by the forms of the app page. A form builds the body of a request from
 * what the person typed, checks it with the request schema of `lib/api/schemas`
 * (which carries the contract's rules) and shows one message per field. The
 * server stays authoritative for what needs its state (a taken slug, the cap on
 * locks).
 */
import type { z } from "zod";

import {
  FORM_ERROR_KEY,
  fieldErrorsFromIssues,
} from "@/lib/forms/field-errors";

export { FORM_ERROR_KEY };

/** One message per field, keyed by the field's name; `FORM_ERROR_KEY` is the form as a whole. */
export type FormErrors = Record<string, string>;

/** What a form's builder returns: the body to send, or what to show next to the fields. */
export type FormResult<Body> =
  { ok: true; body: Body } | { ok: false; errors: FormErrors };

export function formFailure(errors: FormErrors): {
  ok: false;
  errors: FormErrors;
} {
  return { ok: false, errors };
}

/**
 * Zod's default wording for a limit ("Too big: expected string to have <=500
 * characters") reads as an error message of a library. The contract's own
 * messages (the `refine` ones) are kept as they are.
 */
export function friendlyIssueMessage(issue: z.core.$ZodIssue): string {
  if (issue.code === "too_big") {
    if (issue.origin === "string") {
      return `At most ${String(issue.maximum)} characters`;
    }
    if (issue.origin === "array") {
      return `At most ${String(issue.maximum)} items`;
    }
    return `At most ${String(issue.maximum)}`;
  }
  if (issue.code === "too_small") {
    if (issue.origin === "string" && Number(issue.minimum) <= 1) {
      return "Must not be empty";
    }
    if (issue.origin === "string") {
      return `At least ${String(issue.minimum)} characters`;
    }
    return `At least ${String(issue.minimum)}`;
  }
  return issue.message;
}

/**
 * The errors after the person edits `field`: its own message goes, and so does
 * the form-wide one (it described the old values). Other fields keep theirs
 * until they are edited or the form is sent again.
 */
export function withoutError(errors: FormErrors, field: string): FormErrors {
  if (!(field in errors) && !(FORM_ERROR_KEY in errors)) return errors;
  return Object.fromEntries(
    Object.entries(errors).filter(
      ([key]) => key !== field && key !== FORM_ERROR_KEY,
    ),
  );
}

export interface ErrorsFromZodOptions {
  /** A path segment to leave out when it comes first (`payload`, so `payload.slug` is the field `slug`). */
  dropPathPrefix?: string;
  /** Leaves out array positions, so `tags.3` is the field `tags`. */
  dropIndexes?: boolean;
}

/** The failed `safeParse` of a request schema as one message per field. */
export function errorsFromZod(
  error: z.ZodError,
  options: ErrorsFromZodOptions = {},
): FormErrors {
  const { dropPathPrefix, dropIndexes = false } = options;
  return fieldErrorsFromIssues(
    error.issues.map((issue) => {
      const path = issue.path.filter(
        (segment, position) =>
          !(position === 0 && segment === dropPathPrefix) &&
          !(dropIndexes && typeof segment === "number"),
      );
      return { path, message: friendlyIssueMessage(issue) };
    }),
  );
}
