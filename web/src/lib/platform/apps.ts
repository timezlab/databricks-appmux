/**
 * The platform page's app list and its register form (design 10.1, admin-api
 * `POST /apps`). The forms of one app (its platform fields and owner) are on
 * the app's Settings section, in lib/app/settings.ts.
 *
 * The form turns typed text into the body the contract names and lets the
 * request schema of lib/api/schemas judge it, so it cannot send what the
 * server answers 400 for. A slug that another app has is checked here; the
 * server still has the final say, because another admin can take it a second
 * later.
 */
import { RegisterAppRequest } from "@/lib/api/schemas";
import type {
  AppState,
  AppSummary,
  PlatformApp,
  RequestRow,
} from "@/lib/api/schemas";
import { pendingChangeRequestCount } from "@/lib/app/sections";
import {
  formErrors,
  memoryProblem,
  parseWholeNumber,
  slugTaken,
} from "./forms";
import type { FormParse } from "./forms";

// --------------------------------------------------------------------- rows

/** One row of the Apps area: the platform's fields, with what the app list and the requests add. */
export interface PlatformAppRow {
  slug: string;
  displayName: string;
  owner: string;
  memoryBudgetMb: number;
  /** `null` while the app list has not loaded, or before it holds an app just registered. */
  state: AppState | null;
  /** Settings and delegation requests waiting for an answer; `null` while the requests are unknown. */
  pendingRequests: number | null;
}

/**
 * The rows of the Apps area, in the platform's order. `GET /platform` has no
 * state, so it comes from the app list (`GET /apps`), matched by slug; the
 * count is the one the app's own sidebar shows on Requests.
 */
export function platformAppRows(
  apps: readonly PlatformApp[],
  summaries: readonly AppSummary[] | null,
  requests: readonly RequestRow[] | null,
): PlatformAppRow[] {
  const stateOf = new Map(
    (summaries ?? []).map((summary) => [summary.slug, summary.state]),
  );
  return apps.map((app) => ({
    slug: app.slug,
    displayName: app.display_name,
    owner: app.owner,
    memoryBudgetMb: app.memory_budget_mb,
    state: stateOf.get(app.slug) ?? null,
    pendingRequests:
      requests === null ? null : pendingChangeRequestCount(requests, app.slug),
  }));
}

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
  // A blank field leaves the budget out: the app gets the platform default.
  const isMemoryBlank = values.memory_budget_mb.trim() === "";
  const memory = parseWholeNumber(values.memory_budget_mb);
  const parsed = RegisterAppRequest.safeParse({
    slug,
    display_name: values.display_name.trim(),
    source_path: values.source_path.trim(),
    // A NaN is refused by the schema; the message below is the one shown.
    ...(isMemoryBlank ? {} : { memory_budget_mb: memory ?? Number.NaN }),
    owner: values.owner.trim(),
  });

  const errors: Record<string, string> = parsed.success
    ? {}
    : { ...formErrors(parsed.error.issues) };
  if (!isMemoryBlank && memory === null) {
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
