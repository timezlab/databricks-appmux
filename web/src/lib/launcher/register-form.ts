/**
 * The "Request a new app" form (design 9.4, admin-api.md `CreatePermissionRequest`).
 *
 * The rules are the contract's: the form builds the request body and runs it
 * through `CreatePermissionRequest`, the same schema that `lib/api` documents,
 * so a body the server would refuse for its shape never leaves the browser and
 * the two cannot drift. What the schema cannot know is added from data the page
 * already holds (a slug the visitor can see, a request already pending, the cap
 * of 3), and only as a courtesy: the server stays the authority and its refusal
 * is shown as it is.
 */
import {
  FORM_ERROR_KEY,
  fieldErrorsFromIssues,
} from "@/lib/forms/field-errors";
import { CreatePermissionRequest } from "@/lib/api";
import type { RequestRow } from "@/lib/api";
import { REASON_MAX_LENGTH } from "@/lib/api/schemas/primitives";
import { MAX_PENDING_REGISTER_REQUESTS } from "./requests";

// The limit is the schema's (admin-api.md, CreatePermissionRequest.reason),
// read from where it is defined; the test checks both edges against the schema.
// The form has no memory budget: the platform sets one when the request is approved.
export { REASON_MAX_LENGTH };

/** What the inputs hold: text, as typed. */
export interface RegisterFormValues {
  slug: string;
  displayName: string;
  sourcePath: string;
  reason: string;
}

export const EMPTY_REGISTER_FORM: RegisterFormValues = {
  slug: "",
  displayName: "",
  sourcePath: "",
  reason: "",
};

export type RegisterFormField = keyof RegisterFormValues;
export type RegisterFormErrors = Partial<
  Record<RegisterFormField | typeof FORM_ERROR_KEY, string>
>;

/** Facts from the page's own polls; both are courtesy checks (see the file header). */
export interface RegisterFormContext {
  /** Slugs of the apps the visitor can already see. */
  knownSlugs: readonly string[];
  /** Proposed slugs of the visitor's pending register requests, or `null` while the list is not loaded. */
  pendingRegisterSlugs: readonly string[] | null;
}

export type RegisterFormValidation =
  | { ok: true; request: CreatePermissionRequest }
  | { ok: false; errors: RegisterFormErrors };

const FIELDS: readonly RegisterFormField[] = [
  "slug",
  "displayName",
  "sourcePath",
  "reason",
];

function trimmed(values: RegisterFormValues): RegisterFormValues {
  return {
    slug: values.slug.trim(),
    displayName: values.displayName.trim(),
    sourcePath: values.sourcePath.trim(),
    reason: values.reason.trim(),
  };
}

function toCandidate(values: RegisterFormValues): unknown {
  return {
    kind: "register",
    app: values.slug,
    payload: {
      slug: values.slug,
      display_name: values.displayName,
      source_path: values.sourcePath,
    },
    reason: values.reason,
  };
}

// The words for a field the schema refused. The schema's own messages are
// fragments ("must not be blank"); these say what to type.
const WORDING: Record<
  RegisterFormField,
  (values: RegisterFormValues) => string
> = {
  slug: ({ slug }) =>
    slug === ""
      ? "Enter a slug."
      : "Use 1 to 31 lower-case letters, digits or hyphens, starting with a letter or digit.",
  displayName: () => "Enter the name people will see on the launcher.",
  sourcePath: ({ sourcePath }) =>
    sourcePath === ""
      ? "Enter the workspace folder the app is read from."
      : "The folder must start with /Workspace/.",
  reason: ({ reason }) =>
    reason.length > REASON_MAX_LENGTH
      ? `Use at most ${REASON_MAX_LENGTH} characters (now ${reason.length}).`
      : "Say why you need this app, so a platform admin can decide.",
};

// A Map, not an object: a path key such as "constructor" must not find a member of Object.prototype.
const PAYLOAD_FIELD = new Map<PropertyKey, RegisterFormField>([
  ["slug", "slug"],
  ["display_name", "displayName"],
  ["source_path", "sourcePath"],
]);

/** The form field an issue of the request body belongs to, or the form as a whole. */
function fieldOfPath(
  path: readonly PropertyKey[],
): RegisterFormField | typeof FORM_ERROR_KEY {
  const [first, second] = path;
  if (first === "app") return "slug";
  if (first === "reason") return "reason";
  if (first === "payload" && second !== undefined) {
    return PAYLOAD_FIELD.get(second) ?? FORM_ERROR_KEY;
  }
  return FORM_ERROR_KEY;
}

function courtesyErrors(
  values: RegisterFormValues,
  context: RegisterFormContext,
): RegisterFormErrors {
  const errors: RegisterFormErrors = {};
  if (context.knownSlugs.includes(values.slug)) {
    errors.slug = "An app with this slug already exists. Choose another slug.";
  } else if (context.pendingRegisterSlugs?.includes(values.slug) === true) {
    errors.slug =
      "You already asked for this slug and the request is still pending. Withdraw it first to ask again.";
  }
  if (
    context.pendingRegisterSlugs !== null &&
    context.pendingRegisterSlugs.length >= MAX_PENDING_REGISTER_REQUESTS
  ) {
    errors[FORM_ERROR_KEY] =
      `You already have ${MAX_PENDING_REGISTER_REQUESTS} pending requests for a new app. Wait for an answer or withdraw one.`;
  }
  return errors;
}

/** The errors without the one of `field`: the person is editing it, so its old complaint no longer applies. */
export function withoutFieldError(
  errors: RegisterFormErrors,
  field: RegisterFormField,
): RegisterFormErrors {
  return Object.fromEntries(
    Object.entries(errors).filter(([key]) => key !== field),
  );
}

/** Checks the form against the contract's rules and builds the body to send. */
export function validateRegisterForm(
  input: RegisterFormValues,
  context: RegisterFormContext,
): RegisterFormValidation {
  const values = trimmed(input);
  const parsed = CreatePermissionRequest.safeParse(toCandidate(values));

  const errors: RegisterFormErrors = {};
  if (!parsed.success) {
    const byKey = fieldErrorsFromIssues(
      parsed.error.issues.map((issue) => {
        const field = fieldOfPath(issue.path);
        return {
          path: [field],
          message:
            field === FORM_ERROR_KEY ? issue.message : WORDING[field](values),
        };
      }),
    );
    for (const field of FIELDS) {
      const message = byKey[field];
      if (message !== undefined) errors[field] = message;
    }
    const formMessage = byKey[FORM_ERROR_KEY];
    if (formMessage !== undefined) errors[FORM_ERROR_KEY] = formMessage;
  }

  // Courtesy checks come second so a malformed slug is told its pattern, not "taken".
  const extra = courtesyErrors(values, context);
  if (extra.slug !== undefined && errors.slug === undefined)
    errors.slug = extra.slug;
  if (
    extra[FORM_ERROR_KEY] !== undefined &&
    errors[FORM_ERROR_KEY] === undefined
  ) {
    errors[FORM_ERROR_KEY] = extra[FORM_ERROR_KEY];
  }

  if (!parsed.success || Object.keys(errors).length > 0) {
    return { ok: false, errors };
  }
  return { ok: true, request: parsed.data };
}

export type SubmitRegisterResult =
  | { status: "invalid"; errors: RegisterFormErrors }
  | { status: "sent"; request: RequestRow }
  /** The server said no (or could not be reached); `error` is what the call threw, for `ErrorNotice`. */
  | { status: "refused"; error: unknown };

/** Validates, then sends once. Nothing is sent while the form is invalid. */
export async function submitRegisterForm(
  values: RegisterFormValues,
  context: RegisterFormContext,
  send: (body: CreatePermissionRequest) => Promise<RequestRow>,
): Promise<SubmitRegisterResult> {
  const checked = validateRegisterForm(values, context);
  if (!checked.ok) return { status: "invalid", errors: checked.errors };
  try {
    return { status: "sent", request: await send(checked.request) };
  } catch (error) {
    // Returned, not swallowed: the page shows it where the person is looking.
    return { status: "refused", error };
  }
}
