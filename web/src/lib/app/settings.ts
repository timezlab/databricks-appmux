/**
 * The app's Settings section (docs/product/admin-ui-design.md, "The app's
 * sections"): the platform fields (`PATCH /apps/{app}`), the owner
 * (`PUT /apps/{app}/owner`) and delete (`DELETE /apps/{app}`), which only a
 * platform admin changes (design 9.1: `app.edit_platform_fields`,
 * `app.set_owner`, `app.delete`); an app admin sees them read-only and asks
 * for a change of the fields under Requests (design 9.4).
 *
 * Every form turns typed text into the body the contract names and lets the
 * request schema of lib/api/schemas judge it, so a form cannot send what the
 * server answers 400 for. What only the page can know (a slug that another app
 * has, a form where nothing changed) is checked here; the server still has the
 * final say on a slug, because another admin can take it a second later.
 */
import { SetOwnerRequest, UpdateAppRequest } from "@/lib/api/schemas";
import type { AppDetail, Runtime } from "@/lib/api/schemas";
import {
  FORM_ERROR_KEY,
  formErrors,
  formFailure,
  memoryProblem,
  parseWholeNumber,
  slugTaken,
} from "@/lib/platform/forms";
import type { FormParse } from "@/lib/platform/forms";
import type { Actor } from "./actions";
import { REASON_NOT_MANAGER, REASON_UNKNOWN_ACTOR } from "./actions";

/** What the forms read of an app: an `AppDetail` and a platform list row both hold it. */
export type AppFieldsSource = Pick<
  AppDetail,
  | "slug"
  | "display_name"
  | "source_path"
  | "memory_budget_mb"
  | "owner"
  | "version"
>;

// ----------------------------------------------------------- platform fields

export interface AppFieldsValues {
  slug: string;
  display_name: string;
  source_path: string;
  memory_budget_mb: string;
}

export function appFieldsFormValues(app: AppFieldsSource): AppFieldsValues {
  return {
    slug: app.slug,
    display_name: app.display_name,
    source_path: app.source_path,
    memory_budget_mb: String(app.memory_budget_mb),
  };
}

/**
 * The body of `PATCH /apps/{app}`: only the fields that differ from `app`,
 * with `app.version`. `otherSlugs` are the slugs of every other app.
 */
export function parseAppFieldsForm(
  app: AppFieldsSource,
  values: AppFieldsValues,
  otherSlugs: readonly string[],
): FormParse<UpdateAppRequest> {
  const errors: Record<string, string> = {};
  const body: Record<string, unknown> = { version: app.version };

  const slug = values.slug.trim();
  if (slug !== app.slug) body.slug = slug;
  const displayName = values.display_name.trim();
  if (displayName !== app.display_name) body.display_name = displayName;
  const sourcePath = values.source_path.trim();
  if (sourcePath !== app.source_path) body.source_path = sourcePath;
  const memory = parseWholeNumber(values.memory_budget_mb);
  if (memory === null) {
    errors.memory_budget_mb = memoryProblem(values.memory_budget_mb);
  } else if (memory !== app.memory_budget_mb) {
    body.memory_budget_mb = memory;
  }

  const hasChange = Object.keys(body).length > 1;
  if (!hasChange && Object.keys(errors).length === 0) {
    return formFailure("Change at least one field");
  }

  let value: UpdateAppRequest | null = null;
  if (hasChange) {
    const parsed = UpdateAppRequest.safeParse(body);
    if (parsed.success) {
      value = parsed.data;
    } else {
      // The wording chosen above for the budget wins over the schema's.
      Object.assign(errors, { ...formErrors(parsed.error.issues), ...errors });
    }
  }
  if (
    body.slug !== undefined &&
    errors.slug === undefined &&
    otherSlugs.includes(slug)
  ) {
    errors.slug = slugTaken(slug);
  }

  if (value === null || Object.keys(errors).length > 0) {
    return { ok: false, errors };
  }
  return { ok: true, value };
}

// -------------------------------------------------------------------- owner

/** The body of `PUT /apps/{app}/owner`. */
export function parseOwnerForm(
  app: AppFieldsSource,
  text: string,
): FormParse<SetOwnerRequest> {
  const owner = text.trim();
  if (owner === app.owner)
    return { ok: false, errors: { owner: "is already the owner" } };
  const parsed = SetOwnerRequest.safeParse({ version: app.version, owner });
  return parsed.success
    ? { ok: true, value: parsed.data }
    : { ok: false, errors: formErrors(parsed.error.issues) };
}

// ------------------------------------------------------------- one save bar

/** The Settings form of a platform admin: the platform fields and the owner, saved together. */
export interface AppSettingsValues extends AppFieldsValues {
  owner: string;
}

export function appSettingsFormValues(app: AppFieldsSource): AppSettingsValues {
  return { ...appFieldsFormValues(app), owner: app.owner };
}

/**
 * What the form is keyed by in React: the values it starts from. The version is
 * left out on purpose. A write of another kind (a delegation, the egress rules)
 * moves the version, and a form being typed into must not be reset by that; the
 * submit reads the latest version from the polled app.
 */
export function appSettingsKey(app: AppFieldsSource): string {
  return JSON.stringify(Object.values(appSettingsFormValues(app)));
}

/**
 * What one Save sends: the body of `PATCH /apps/{app}` when a field changed,
 * and the new owner for `PUT /apps/{app}/owner` when it changed. The owner's
 * version is taken when it is sent, since a PATCH before it moves the version.
 */
export interface SettingsChange {
  fields: UpdateAppRequest | null;
  owner: string | null;
}

export function parseSettingsForm(
  app: AppFieldsSource,
  values: AppSettingsValues,
  otherSlugs: readonly string[],
): FormParse<SettingsChange> {
  const { owner: ownerText, ...fieldValues } = values;
  const fields = parseAppFieldsForm(app, fieldValues, otherSlugs);
  // `parseAppFieldsForm` refuses with the form-wide message only when no field changed and none is wrong.
  const isFieldsUnchanged = !fields.ok && FORM_ERROR_KEY in fields.errors;
  const isOwnerUnchanged = ownerText.trim() === app.owner;

  if (isFieldsUnchanged && isOwnerUnchanged) {
    return formFailure("Change at least one field");
  }
  const owner = isOwnerUnchanged ? null : parseOwnerForm(app, ownerText);
  const errors = {
    ...(fields.ok || isFieldsUnchanged ? {} : fields.errors),
    ...(owner === null || owner.ok ? {} : owner.errors),
  };
  if (Object.keys(errors).length > 0) return { ok: false, errors };
  return {
    ok: true,
    value: {
      fields: fields.ok ? fields.value : null,
      owner: owner?.ok === true ? owner.value.owner : null,
    },
  };
}

// ------------------------------------------------------- carried outcomes

/** What a rename or a delete says on the page it lands on. */
export interface CarriedOutcome {
  title: string;
  detail: string | null;
}

// A rename moves the page to the new slug, and the app's shell is keyed by
// slug, so the notice it held goes with the old shell; a delete leaves for the
// platform's list of apps. The outcome is carried across that one client-side
// navigation here, read by the page it lands on when it first renders, and
// dropped once that page is on screen (reading and dropping are two steps
// because React may render a page twice before it mounts it). A full reload
// drops it too, which is right: by then the person has seen the result.
const carriedOutcomes = new Map<string, CarriedOutcome>();

/** Leaves `outcome` for the page at `path` (as `appPagePath` writes it). */
export function carryOutcome(path: string, outcome: CarriedOutcome): void {
  carriedOutcomes.set(path, outcome);
}

/** The outcome left for `path`; `null` when none waits. */
export function carriedOutcome(path: string): CarriedOutcome | null {
  return carriedOutcomes.get(path) ?? null;
}

/** Called once the page at `path` shows its outcome, so a later visit does not show it again. */
export function dropCarriedOutcome(path: string): void {
  carriedOutcomes.delete(path);
}

// ------------------------------------------------------------------- rename

// A running app moves to the new prefix through the health-gated swap (design
// 5.5), so the old URL stops answering only when the new copy has passed its
// health check, and not before.
function swapSentence(to: string): string {
  return `A running app is restarted under /${to}/, and the old address answers 404 once the new copy passes its health check.`;
}

/** Said beside Save before a changed slug is sent: the route moves. */
export function renameWarning(from: string, to: string): string {
  return `Changing the slug moves the app from /${from}/ to /${to}/. ${swapSentence(to)} Links and bookmarks to the old address stop working.`;
}

/** What to tell the person once `PATCH /apps/{app}` answered with a new slug: the row holds it. */
export function renameMessage(from: string, to: string): string {
  return `Renamed ${from} to ${to}. The app's URL prefix is saved as /${to}/. ${swapSentence(to)}`;
}

// ------------------------------------------------------------------- access

/**
 * How a person meets one part of Settings: they change it; they see it
 * read-only and may ask a platform admin for a change (Requests); or they only
 * see it, with the reason.
 */
export type SettingsPartAccess =
  | { kind: "edit" }
  | { kind: "request" }
  | { kind: "read-only"; reason: string };

export interface SettingsAccess {
  /** Name, slug, source folder and memory budget. */
  fields: SettingsPartAccess;
  owner: SettingsPartAccess;
  delete: SettingsPartAccess;
}

export const REASON_OWNER_PLATFORM_ADMIN =
  "Only a platform admin changes the owner.";
export const REASON_DELETE_PLATFORM_ADMIN =
  "Only a platform admin deletes an app.";

/**
 * Who may change what (design 9.1): a platform admin all of it; an app admin
 * asks for the fields (the owner is not part of a settings request, and
 * deleting is not requestable); anyone else, or anyone while the actor is
 * unknown, only sees them. The server checks every call again.
 */
export function settingsAccess(actor: Actor | null): SettingsAccess {
  if (actor === null) return everyPart(REASON_UNKNOWN_ACTOR);
  if (actor.isPlatformAdmin) {
    return {
      fields: { kind: "edit" },
      owner: { kind: "edit" },
      delete: { kind: "edit" },
    };
  }
  if (!actor.canManage) return everyPart(REASON_NOT_MANAGER);
  return {
    fields: { kind: "request" },
    owner: { kind: "read-only", reason: REASON_OWNER_PLATFORM_ADMIN },
    delete: { kind: "read-only", reason: REASON_DELETE_PLATFORM_ADMIN },
  };
}

function everyPart(reason: string): SettingsAccess {
  const readOnly: SettingsPartAccess = { kind: "read-only", reason };
  return { fields: readOnly, owner: readOnly, delete: readOnly };
}

// ------------------------------------------------------------------- memory

/**
 * The budget a Node.js app needs to build and start: the figure the server
 * sends as `WorkspaceListing.suggested_memory_mb` for a Node.js folder
 * (admin-api.md addendum, `GET /workspace/browse`). The app's own page has no
 * listing to read it from, so it is written once here.
 */
export const NODE_MEMORY_MIN_MB = 768;

/**
 * The sentence beside the memory budget of a Node.js app whose budget is below
 * what Node.js needs; `null` otherwise, and for a budget that is not a number
 * (the field's own error says what is wrong with it).
 */
export function nodeMemoryHint(
  runtime: Runtime | null,
  budgetText: string,
): string | null {
  if (runtime !== "node") return null;
  const budget = parseWholeNumber(budgetText);
  if (budget === null || budget >= NODE_MEMORY_MIN_MB) return null;
  return `This is a Node.js app. Node.js needs about ${NODE_MEMORY_MIN_MB} MB of memory; with ${budget} MB it may fail to build or start.`;
}

// ------------------------------------------------------------------- delete

/** Delete asks for the slug typed out, so a press on the wrong app's page does nothing. */
export function isDeleteConfirmed(typed: string, slug: string): boolean {
  return typed.trim() === slug;
}
