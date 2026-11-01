/**
 * The platform page's app list and its forms: register an app, edit the
 * platform fields, set the owner (design 10.1, admin-api `POST /apps`,
 * `PATCH /apps/{app}`, `PUT /apps/{app}/owner`).
 *
 * Every form turns typed text into the body the contract names and lets the
 * request schema of lib/api/schemas judge it, so a form cannot send what the
 * server answers 400 for. What only the page can know (a slug that another app
 * has, a form where nothing changed) is checked here; the server still has the
 * final say on a slug, because another admin can take it a second later.
 */
import {
  RegisterAppRequest,
  SetOwnerRequest,
  UpdateAppRequest,
} from "@/lib/api/schemas";
import type { PlatformApp } from "@/lib/api/schemas";
import type { ActionOutcome } from "./action";
import { formErrors, formFailure, parseWholeNumber } from "./forms";
import type { FormParse } from "./forms";

// ------------------------------------------------------------------- filter

/** Apps whose slug, display name or owner contains `query` (case-insensitive); all of them for a blank query. */
export function filterApps<
  T extends Pick<PlatformApp, "slug" | "display_name" | "owner">,
>(apps: readonly T[], query: string): T[] {
  const needle = query.trim().toLowerCase();
  if (needle === "") return [...apps];
  return apps.filter((app) =>
    [app.slug, app.display_name, app.owner].some((text) =>
      text.toLowerCase().includes(needle),
    ),
  );
}

// ------------------------------------------------------------------ helpers

function memoryProblem(text: string): string {
  return text.trim() === "" ? "is required" : "must be a whole number of MB";
}

function slugTaken(slug: string): string {
  return `slug '${slug}' is already taken`;
}

// ----------------------------------------------------------------- register

/** The text of the register form, keyed by the API's field names (so an error finds its field). */
export interface RegisterFormValues {
  slug: string;
  display_name: string;
  source_path: string;
  memory_budget_mb: string;
  owner: string;
}

export const EMPTY_REGISTER_FORM: RegisterFormValues = {
  slug: "",
  display_name: "",
  source_path: "",
  memory_budget_mb: "",
  owner: "",
};

export function parseRegisterForm(
  values: RegisterFormValues,
  existingSlugs: readonly string[],
): FormParse<RegisterAppRequest> {
  const slug = values.slug.trim();
  const memory = parseWholeNumber(values.memory_budget_mb);
  const parsed = RegisterAppRequest.safeParse({
    slug,
    display_name: values.display_name.trim(),
    source_path: values.source_path.trim(),
    // A NaN is refused by the schema; the message below is the one shown.
    memory_budget_mb: memory ?? Number.NaN,
    owner: values.owner.trim(),
  });

  const errors: Record<string, string> = parsed.success
    ? {}
    : { ...formErrors(parsed.error.issues) };
  if (memory === null) {
    errors.memory_budget_mb = memoryProblem(values.memory_budget_mb);
  }
  if (errors.slug === undefined && existingSlugs.includes(slug)) {
    errors.slug = slugTaken(slug);
  }
  if (!parsed.success || Object.keys(errors).length > 0) {
    return { ok: false, errors };
  }
  return { ok: true, value: parsed.data };
}

// ----------------------------------------------------------- platform fields

export interface AppFieldsValues {
  slug: string;
  display_name: string;
  source_path: string;
  memory_budget_mb: string;
}

export function appFieldsFormValues(app: PlatformApp): AppFieldsValues {
  return {
    slug: app.slug,
    display_name: app.display_name,
    source_path: app.source_path,
    memory_budget_mb: String(app.memory_budget_mb),
  };
}

/**
 * What a form is keyed by in React: the values it starts from. The version is
 * left out on purpose. A write of another kind (a delegation, the egress rules)
 * moves the version, and a form being typed into must not be reset by that; the
 * submit reads the latest version from the polled app.
 */
export function appFieldsKey(app: PlatformApp): string {
  return JSON.stringify(Object.values(appFieldsFormValues(app)));
}

export function appOwnerKey(app: PlatformApp): string {
  return JSON.stringify([app.slug, app.owner]);
}

/**
 * The body of `PATCH /apps/{app}`: only the fields that differ from `app`,
 * with `app.version`. `otherSlugs` are the slugs of every other app.
 */
export function parseAppFieldsForm(
  app: PlatformApp,
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
  app: PlatformApp,
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

// ------------------------------------------------------------------- rename

/**
 * What to tell the person once `PATCH /apps/{app}` answered with a new slug.
 * The answer means the row holds the new slug. A running app moves to the new
 * prefix through the health-gated swap (design 5.5), so the old URL stops
 * answering only when the new copy has passed its health check, and not before.
 */
export function renameOutcome(from: string, to: string): ActionOutcome {
  return {
    tone: "success",
    title: `Renamed ${from} to ${to}`,
    detail: `The app's URL prefix is saved as /${to}/. A running app is restarted under it, and the old URL answers 404 once the new copy passes its health check.`,
  };
}
