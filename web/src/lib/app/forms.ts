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

// ------------------------------------------------------- suggestion search

/**
 * How long a field waits after the last keystroke before it asks the server
 * for suggestions: long enough that a word typed at speed is one call, short
 * enough that the list follows the typing.
 */
export const SUGGESTION_DELAY_MS = 250;

export type SearchOutcome<Result> =
  | { status: "loading"; text: string }
  | { status: "done"; text: string; result: Result }
  | { status: "failed"; text: string; error: unknown };

export interface DebouncedSearch {
  /** Searches for `text` after the delay; a newer call replaces a waiting one and aborts one in flight. */
  search: (text: string) => void;
  /** Stops a waiting or running search; nothing is reported after it (for unmount and close). */
  cancel: () => void;
}

export interface DebouncedSearchOptions<Result> {
  delayMs: number;
  /** The call; it must give up when `signal` aborts. */
  run: (text: string, signal: AbortSignal) => Promise<Result>;
  onOutcome: (outcome: SearchOutcome<Result>) => void;
}

/**
 * The suggestion fields' search (the principal field, the resource object
 * field): it debounces the keystrokes and keeps only the latest call, so an
 * answer that arrives late for an older text can never replace a newer list.
 * Plain timers and an `AbortController`, with no React in it, so the timing is
 * tested with fake timers.
 */
export function createDebouncedSearch<Result>({
  delayMs,
  run,
  onOutcome,
}: DebouncedSearchOptions<Result>): DebouncedSearch {
  let timer: ReturnType<typeof setTimeout> | null = null;
  let inFlight: AbortController | null = null;

  function cancel(): void {
    if (timer !== null) clearTimeout(timer);
    timer = null;
    inFlight?.abort();
    inFlight = null;
  }

  function start(text: string): void {
    timer = null;
    const controller = new AbortController();
    inFlight = controller;
    run(text, controller.signal).then(
      (result) => {
        if (controller.signal.aborted) return;
        inFlight = null;
        onOutcome({ status: "done", text, result });
      },
      (error: unknown) => {
        // An aborted call was replaced or cancelled on purpose; whatever it
        // rejects with (an AbortError) is not a failure to show.
        if (controller.signal.aborted) return;
        inFlight = null;
        onOutcome({ status: "failed", text, error });
      },
    );
  }

  return {
    search(text) {
      cancel();
      onOutcome({ status: "loading", text });
      timer = setTimeout(() => {
        start(text);
      }, delayMs);
    },
    cancel,
  };
}
