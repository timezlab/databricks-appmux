/**
 * The app page's two requests to a platform admin: a change of the app's
 * settings (slug, name, source folder, memory budget) and a delegation (OBO,
 * shared service principal). Design 9.4: the form leaves out every field that
 * did not change, refuses a request with none left, and the contract's
 * `CreatePermissionRequest` checks the rest. What needs the server's state (a
 * taken slug, free memory) is left to it, and its refusal is shown as it is.
 */
import { CreatePermissionRequest } from "@/lib/api";
import {
  MEMORY_BUDGET_MAX_MB,
  MEMORY_BUDGET_MIN_MB,
  REASON_MAX_LENGTH,
} from "@/lib/api/schemas/primitives";
import type { AppDetail, DelegationKind, RequestRow } from "@/lib/api";
import type { Actor, Availability } from "./actions";
import { REASON_NOT_MANAGER, REASON_UNKNOWN_ACTOR } from "./actions";
import { FORM_ERROR_KEY, errorsFromZod, formFailure } from "./forms";
import type { FormErrors, FormResult } from "./forms";
import { assertNever } from "@/lib/assert-never";
import { formatMegabytes } from "@/lib/format";
import { appRequests } from "@/lib/request-views";

// The limits are the schema's (admin-api.md, RegisterAppRequest and
// CreatePermissionRequest), read from where they are defined; the test checks
// both edges of each against the schema.
export { MEMORY_BUDGET_MAX_MB, MEMORY_BUDGET_MIN_MB };
export const REQUEST_REASON_MAX_LENGTH = REASON_MAX_LENGTH;

/** How many of the app's requests are listed before "show all". */
export const RECENT_REQUESTS_SHOWN = 5;

const WHOLE_NUMBER = /^\d+$/;
const REASON_REQUIRED = "Enter a reason; a platform admin reads it.";

// ---------------------------------------------------------------- settings

/** What the inputs hold: text, as typed. */
export interface SettingsFormValues {
  slug: string;
  displayName: string;
  sourcePath: string;
  memoryBudgetMb: string;
  reason: string;
}

export type SettingsFormField = keyof SettingsFormValues;

export function settingsFormDefaults(app: AppDetail): SettingsFormValues {
  return {
    slug: app.slug,
    displayName: app.display_name,
    sourcePath: app.source_path,
    memoryBudgetMb: String(app.memory_budget_mb),
    reason: "",
  };
}

// The contract's field names to the form's.
const SETTINGS_FIELD_BY_SCHEMA_KEY: Record<string, SettingsFormField> = {
  slug: "slug",
  display_name: "displayName",
  source_path: "sourcePath",
  memory_budget_mb: "memoryBudgetMb",
  reason: "reason",
};

function renameSettingsErrors(errors: FormErrors): FormErrors {
  return Object.fromEntries(
    Object.entries(errors).map(([key, message]) => [
      SETTINGS_FIELD_BY_SCHEMA_KEY[key] ?? key,
      message,
    ]),
  );
}

/**
 * The request for the fields that differ from the app's current values, or the
 * message for each field that does not pass.
 */
export function buildSettingsRequest(
  app: AppDetail,
  values: SettingsFormValues,
): FormResult<CreatePermissionRequest> {
  const slug = values.slug.trim();
  const displayName = values.displayName.trim();
  const sourcePath = values.sourcePath.trim();
  const memoryBudgetText = values.memoryBudgetMb.trim();
  const reason = values.reason.trim();

  const payload: Record<string, string | number> = {};
  const ownErrors: FormErrors = {};
  if (slug !== app.slug) payload.slug = slug;
  if (displayName !== app.display_name) payload.display_name = displayName;
  if (sourcePath !== app.source_path) payload.source_path = sourcePath;
  if (memoryBudgetText !== String(app.memory_budget_mb)) {
    if (WHOLE_NUMBER.test(memoryBudgetText)) {
      payload.memory_budget_mb = Number(memoryBudgetText);
    } else {
      ownErrors.memoryBudgetMb =
        memoryBudgetText === ""
          ? "Enter the memory budget in MB."
          : "Use a whole number of MB, such as 256.";
    }
  }
  if (reason === "") ownErrors.reason = REASON_REQUIRED;

  const parsed = CreatePermissionRequest.safeParse({
    kind: "settings",
    app: app.slug,
    payload,
    reason,
  });
  const schemaErrors = parsed.success
    ? {}
    : renameSettingsErrors(
        errorsFromZod(parsed.error, { dropPathPrefix: "payload" }),
      );

  // The schema refuses an empty payload in its own words; the form says it
  // once, and only when nothing else is wrong with a field.
  delete schemaErrors[FORM_ERROR_KEY];
  const errors: FormErrors = { ...schemaErrors, ...ownErrors };
  if (
    Object.keys(payload).length === 0 &&
    Object.keys(ownErrors).length === 0
  ) {
    errors[FORM_ERROR_KEY] = "Change at least one field before asking.";
  }

  if (parsed.success && Object.keys(errors).length === 0) {
    return { ok: true, body: parsed.data };
  }
  return formFailure(errors);
}

// -------------------------------------------------------------- delegation

export interface DelegationFormValues {
  obo: boolean;
  sharedSp: boolean;
  reason: string;
}

export const EMPTY_DELEGATION_FORM: DelegationFormValues = {
  obo: false,
  sharedSp: false,
  reason: "",
};

export type DelegationChoice =
  { state: "available" } | { state: "delegated" } | { state: "not_allowed" };

export interface DelegationChoices {
  obo: DelegationChoice;
  sharedSp: DelegationChoice;
}

/** What each delegation can be at this moment: asked for, already granted, or switched off by the platform. */
export function delegationChoices(app: AppDetail): DelegationChoices {
  const { obo_delegated, shared_sp_delegated, shared_sp_delegation_allowed } =
    app.delegation;
  return {
    obo: obo_delegated ? { state: "delegated" } : { state: "available" },
    sharedSp: shared_sp_delegated
      ? { state: "delegated" }
      : shared_sp_delegation_allowed
        ? { state: "available" }
        : { state: "not_allowed" },
  };
}

export interface IdentityMode {
  id: "obo" | "shared_sp" | "own_sp";
  label: string;
  isInEffect: boolean;
  /** The state in a word or two, beside the lamp. */
  stateText: string;
  /** What the mode means for the app. */
  description: string;
  /** How to get it when it is not in effect; `null` when it is, or when nothing can be done. */
  howToGet: string | null;
}

const ASK_FOR_DELEGATION =
  "An admin of this app can ask for it with Request a delegation; a platform admin decides.";

/**
 * The identities the app can reach Databricks with, and which are in effect
 * (design 8.1): OBO and the shared service principal are delegated by a
 * platform admin; the app's own service principal needs only its two keys.
 */
export function identityModes(app: AppDetail): IdentityMode[] {
  const choices = delegationChoices(app);
  const hasOwnSpKeys =
    app.config.own_sp_client_id_key !== null &&
    app.config.own_sp_client_secret_key !== null;

  const sharedSpState = choices.sharedSp.state;
  return [
    {
      id: "obo",
      label: "As the visitor (OBO)",
      isInEffect: choices.obo.state === "delegated",
      stateText:
        choices.obo.state === "delegated" ? "Delegated" : "Not delegated",
      description:
        "The app uses each visitor's own Databricks rights, within the appmux App's scopes, while the visitor is on the page.",
      howToGet: choices.obo.state === "delegated" ? null : ASK_FOR_DELEGATION,
    },
    {
      id: "shared_sp",
      label: "As the shared service principal",
      isInEffect: sharedSpState === "delegated",
      stateText:
        sharedSpState === "delegated"
          ? "Delegated"
          : sharedSpState === "not_allowed"
            ? "Switched off by the platform"
            : "Not delegated",
      description:
        "The app borrows the identity appmux itself runs as, as a token that lasts an hour; it works without a visitor.",
      howToGet:
        sharedSpState === "delegated"
          ? null
          : sharedSpState === "not_allowed"
            ? "A platform admin has switched this delegation off for every app, so a request for it is refused."
            : ASK_FOR_DELEGATION,
    },
    {
      id: "own_sp",
      label: "As the app's own service principal",
      isInEffect: hasOwnSpKeys,
      stateText: hasOwnSpKeys ? "Configured" : "Not configured",
      description:
        "The app uses a service principal of its own, from two attached secrets; no delegation is needed.",
      howToGet: hasOwnSpKeys
        ? null
        : "Attach its client id and client secret as secrets under Resources, then name the two keys in the app's settings.",
    },
  ];
}

const DELEGATION_KIND_LABEL: Record<DelegationKind, string> = {
  obo: "Act on behalf of users (OBO)",
  shared_sp: "Act as the shared service principal",
};

function delegationChoiceError(choice: DelegationChoice): string | null {
  switch (choice.state) {
    case "available":
      return null;
    case "delegated":
      return "Already delegated.";
    case "not_allowed":
      return "Not allowed: a platform admin has switched this delegation off.";
    default:
      return assertNever(choice);
  }
}

export function buildDelegationRequest(
  app: AppDetail,
  values: DelegationFormValues,
): FormResult<CreatePermissionRequest> {
  const choices = delegationChoices(app);
  const reason = values.reason.trim();
  const ownErrors: FormErrors = {};

  if (values.obo) {
    const message = delegationChoiceError(choices.obo);
    if (message !== null) ownErrors.obo = message;
  }
  if (values.sharedSp) {
    const message = delegationChoiceError(choices.sharedSp);
    if (message !== null) ownErrors.sharedSp = message;
  }
  if (!values.obo && !values.sharedSp) {
    ownErrors[FORM_ERROR_KEY] = "Choose at least one delegation to ask for.";
  }
  if (reason === "") ownErrors.reason = REASON_REQUIRED;

  const payload: Record<string, true> = {};
  if (values.obo) payload.obo = true;
  if (values.sharedSp) payload.shared_sp = true;
  const parsed = CreatePermissionRequest.safeParse({
    kind: "delegation",
    app: app.slug,
    payload,
    reason,
  });
  const schemaErrors = parsed.success
    ? {}
    : errorsFromZod(parsed.error, { dropPathPrefix: "payload" });
  delete schemaErrors[FORM_ERROR_KEY];
  const errors: FormErrors = { ...schemaErrors, ...ownErrors };

  if (parsed.success && Object.keys(errors).length === 0) {
    return { ok: true, body: parsed.data };
  }
  return formFailure(errors);
}

// ---------------------------------------------------------------- requests

export type AppRequestKind = Extract<
  RequestRow["kind"],
  "settings" | "delegation"
>;

/**
 * The requests this page lists: settings changes and delegations for the app,
 * newest first. The request that registered the app belongs to the launcher.
 */
export function appChangeRequests(
  items: readonly RequestRow[],
  slug: string,
): RequestRow[] {
  return appRequests(items, slug).filter(
    (request) => request.kind !== "register",
  );
}

export function requestKindLabel(kind: RequestRow["kind"]): string {
  switch (kind) {
    case "settings":
      return "Settings change";
    case "delegation":
      return "Delegation";
    case "register":
      return "New app";
    default:
      return assertNever(kind);
  }
}

/** At most one request per (app, kind) is pending (design 9.4), so there is one or none. */
export function pendingRequestOfKind(
  requests: readonly RequestRow[],
  slug: string,
  kind: AppRequestKind,
): RequestRow | null {
  return (
    requests.find(
      (request) =>
        request.app_slug === slug &&
        request.kind === kind &&
        request.status === "pending",
    ) ?? null
  );
}

/**
 * Whether the form of `kind` may be used. `requests` is `null` while the list
 * loads: the form stays closed rather than risk a refusal from the server.
 */
export function requestFormAvailability(
  kind: AppRequestKind,
  slug: string,
  requests: readonly RequestRow[] | null,
  actor: Actor | null,
): Availability {
  if (actor === null) return { isAllowed: false, reason: REASON_UNKNOWN_ACTOR };
  if (!actor.canManage) return { isAllowed: false, reason: REASON_NOT_MANAGER };
  if (requests === null) {
    return { isAllowed: false, reason: "Loading the requests of this app..." };
  }
  if (pendingRequestOfKind(requests, slug, kind) !== null) {
    return {
      isAllowed: false,
      reason:
        "A request of this kind is waiting for a platform admin. Withdraw it to ask again.",
    };
  }
  return { isAllowed: true };
}

/** Only the asker withdraws, and only while the request is pending. */
export function canWithdraw(
  request: RequestRow,
  actorEmail: string | null,
): boolean {
  return (
    request.status === "pending" &&
    actorEmail !== null &&
    request.requested_by === actorEmail
  );
}

const STATUS_WORD: Record<RequestRow["status"], string> = {
  pending: "Waiting for a platform admin",
  approved: "Approved",
  denied: "Denied",
  withdrawn: "Withdrawn",
};

/** The answer column: who closed the request and what they wrote. */
export function requestAnswerText(request: RequestRow): string {
  const word = STATUS_WORD[request.status];
  if (request.status === "pending") return word;
  const by = request.closed_by === null ? "" : ` by ${request.closed_by}`;
  const note =
    request.closed_note === null || request.closed_note === ""
      ? ""
      : `: ${request.closed_note}`;
  return `${word}${by}${note}`;
}

const SETTINGS_LABEL: readonly {
  key: string;
  label: string;
  format: (value: string | number) => string;
}[] = [
  { key: "slug", label: "Slug", format: String },
  { key: "display_name", label: "Display name", format: String },
  { key: "source_path", label: "Source folder", format: String },
  {
    key: "memory_budget_mb",
    label: "Memory budget",
    format: (value) => formatMegabytes(Number(value)),
  },
];

function plainValue(value: unknown): string | number | null {
  if (typeof value === "string") return value;
  if (typeof value === "number" && Number.isFinite(value)) return value;
  return null;
}

/**
 * What a request asks, one line per field, with the current value beside the
 * asked one when the server sent it. Keys this page does not know are left
 * out, and so are values that are not plain text or numbers.
 */
export function requestSummaryLines(request: RequestRow): string[] {
  if (request.kind === "delegation") {
    return (["obo", "shared_sp"] as const)
      .filter((kind) => request.payload[kind] === true)
      .map((kind) => DELEGATION_KIND_LABEL[kind]);
  }
  if (request.kind !== "settings") return [];

  const lines: string[] = [];
  for (const { key, label, format } of SETTINGS_LABEL) {
    const asked = plainValue(request.payload[key]);
    if (asked === null) continue;
    const current = plainValue(request.current?.[key]);
    lines.push(
      current === null
        ? `${label}: ${format(asked)}`
        : `${label}: ${format(current)} to ${format(asked)}`,
    );
  }
  return lines;
}

export function limitRequests<Row>(
  rows: readonly Row[],
  limit: number,
): { shown: Row[]; hiddenCount: number } {
  return {
    shown: rows.slice(0, limit),
    hiddenCount: Math.max(rows.length - limit, 0),
  };
}
