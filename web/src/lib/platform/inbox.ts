/**
 * The platform admin's inbox (design 9.4, admin-api `GET /requests`,
 * `POST /requests/{id}/approve` and `/deny`): what a pending request asks, put
 * next to what is true now, and the body of the answer.
 *
 * A request is written by someone else, and a shared-SP child can write one in
 * anyone's name (design premise 2), so everything in it is shown as text and
 * made visible: control and direction characters become `<U+202E>`, a long value
 * is clipped with a note saying so, and a key this page does not know is shown,
 * never dropped. What the admin approves is what the admin read.
 *
 * Warnings only repeat what the server would refuse or do (a taken slug closes
 * the request as denied; a shared-SP grant fails while the master switch is
 * off). They are advice before the click; the server decides.
 */
import { DecidePermissionRequest } from "@/lib/api/schemas";
import type { PlatformApp, RequestKind, RequestRow } from "@/lib/api/schemas";
import { UNKNOWN_TEXT, formatCount, formatMegabytes } from "@/lib/format";
import type { ActionOutcome } from "./action";
import { DELEGATION_INFO } from "./delegations";
import { formErrors } from "./forms";
import type { FormParse } from "./forms";

/** Longest value or app text shown whole; a longer one is clipped with a count of what is missing. */
const MAX_VALUE_CHARACTERS = 300;
const MAX_REASON_CHARACTERS = 1000;

const MISSING_TEXT = "(missing)";

// ------------------------------------------------------------------ text

const LINE_BREAK = 0x0a;
const TAB = 0x09;

function isInvisibleOrControl(codePoint: number): boolean {
  return (
    codePoint < 0x20 ||
    (codePoint >= 0x7f && codePoint <= 0x9f) ||
    (codePoint >= 0x200b && codePoint <= 0x200f) ||
    (codePoint >= 0x2028 && codePoint <= 0x202e) ||
    (codePoint >= 0x2060 && codePoint <= 0x2069) ||
    codePoint === 0xfeff
  );
}

export interface VisibleTextOptions {
  /** Keep `\n` and tab (for a reason shown in a block that preserves them). */
  keepLineBreaks?: boolean;
}

/** `text` with every control, zero-width and direction character written out as `<U+XXXX>`. */
export function visibleText(
  text: string,
  { keepLineBreaks = false }: VisibleTextOptions = {},
): string {
  let result = "";
  for (const character of text) {
    const codePoint = character.codePointAt(0) ?? 0;
    const isKept =
      keepLineBreaks && (codePoint === LINE_BREAK || codePoint === TAB);
    result +=
      isInvisibleOrControl(codePoint) && !isKept
        ? `<U+${codePoint.toString(16).toUpperCase().padStart(4, "0")}>`
        : character;
  }
  return result;
}

function clip(text: string, limit: number): string {
  // Past `limit` characters nothing is kept, so a huge value costs no more than a big one.
  if (text.length <= limit) return text;
  const characters = Array.from(text);
  if (characters.length <= limit) return text;
  const hidden = formatCount(characters.length - limit);
  return `${characters.slice(0, limit).join("")}… (${hidden} more characters)`;
}

function safeText(
  text: string,
  limit: number,
  options?: VisibleTextOptions,
): string {
  return visibleText(clip(text, limit), options);
}

// ----------------------------------------------------------------- fields

type FieldName =
  | "slug"
  | "display_name"
  | "source_path"
  | "memory_budget_mb"
  | "obo"
  | "shared_sp";

const FIELD_LABEL: Record<FieldName, string> = {
  slug: "Slug (URL prefix)",
  display_name: "Display name",
  source_path: "Source folder",
  memory_budget_mb: "Memory budget",
  obo: "OBO delegation",
  shared_sp: "Shared-SP delegation",
};

const APP_FIELDS: readonly FieldName[] = [
  "slug",
  "display_name",
  "source_path",
  "memory_budget_mb",
];
const DELEGATION_FIELDS: readonly FieldName[] = ["obo", "shared_sp"];

const KIND_FIELDS: Record<RequestKind, readonly FieldName[]> = {
  register: APP_FIELDS,
  settings: APP_FIELDS,
  delegation: DELEGATION_FIELDS,
};

const KIND_LABEL: Record<RequestKind, string> = {
  register: "Register an app",
  delegation: "Delegation",
  settings: "Settings change",
};

/** Design 9.3: the whole `register` request, `source_path`, `obo` and `shared_sp`. */
function isGateField(kind: RequestKind, field: string): boolean {
  return (
    kind === "register" ||
    field === "source_path" ||
    field === "obo" ||
    field === "shared_sp"
  );
}

function isKnownField(kind: RequestKind, field: string): boolean {
  return KIND_FIELDS[kind].some((known) => known === field);
}

function isDelegationField(field: string): field is "obo" | "shared_sp" {
  return field === "obo" || field === "shared_sp";
}

// ------------------------------------------------------------------- items

export interface InboxChange {
  field: string;
  label: string;
  /** `null` when there is no current value (a registration). */
  current: string | null;
  requested: string;
  isGate: boolean;
  /** A line about what this field means or does, or `null`. */
  note: string | null;
  /** The key is not one this page knows; it is shown so it can be read. */
  isUnknown: boolean;
}

export interface InboxItem {
  id: number;
  kind: RequestKind;
  kindLabel: string;
  appText: string;
  requestedBy: string;
  /** The time as the server sent it; shown with `RelativeTime`. */
  requestedAt: string;
  reason: string;
  changes: InboxChange[];
  warnings: string[];
  /** Approving needs a reason (the request holds a gate field). */
  requiresReason: boolean;
}

export interface InboxContext {
  apps: readonly Pick<PlatformApp, "slug">[];
  isSharedSpAllowed: boolean;
  /** `PlatformView.memory.free_mb`; negative when the slot is over-committed. */
  freeMemoryMb: number;
  /**
   * `PlatformView.settings.default_memory_budget_mb`: the budget approving a
   * registration gives the new app. A registration does not ask for one.
   */
  defaultMemoryBudgetMb: number;
}

function formatValue(field: string, value: unknown): string {
  if (value === undefined || value === null) return UNKNOWN_TEXT;
  if (isDelegationField(field) && typeof value === "boolean") {
    return value ? "allowed" : "not allowed";
  }
  if (field === "memory_budget_mb" && typeof value === "number") {
    return formatMegabytes(value);
  }
  if (typeof value === "string") return safeText(value, MAX_VALUE_CHARACTERS);
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return safeText(JSON.stringify(value) ?? UNKNOWN_TEXT, MAX_VALUE_CHARACTERS);
}

function ownValue(
  record: Readonly<Record<string, unknown>> | null,
  key: string,
): { has: boolean; value: unknown } {
  if (record === null || !Object.hasOwn(record, key)) {
    return { has: false, value: undefined };
  }
  return { has: true, value: record[key] };
}

function freeMemoryNote(freeMemoryMb: number): string {
  return freeMemoryMb >= 0
    ? `${formatMegabytes(freeMemoryMb)} free in the slot now`
    : `The slot is over-committed by ${formatMegabytes(-freeMemoryMb)}`;
}

function noteFor(
  kind: RequestKind,
  field: string,
  context: InboxContext,
): string | null {
  if (field === "slug") {
    return kind === "settings"
      ? "Moves the app to a new URL; the old URL answers 404."
      : null;
  }
  if (field === "source_path") return "Decides what code runs in the slot.";
  if (field === "memory_budget_mb") {
    const free = freeMemoryNote(context.freeMemoryMb);
    return kind === "register"
      ? `The platform default; the asker does not choose it. ${free}`
      : free;
  }
  if (isDelegationField(field)) return DELEGATION_INFO[field].grantEffect;
  return null;
}

function buildChanges(row: RequestRow, context: InboxContext): InboxChange[] {
  const changes: InboxChange[] = [];

  for (const field of KIND_FIELDS[row.kind]) {
    // Approving a registration gives the new app the platform default; a budget
    // an older request still carries is ignored, so it is not shown.
    const asked =
      row.kind === "register" && field === "memory_budget_mb"
        ? { has: true, value: context.defaultMemoryBudgetMb }
        : ownValue(row.payload, field);
    // A settings or delegation request leaves unasked fields out (null); a registration always shows all four.
    if (row.kind !== "register" && (asked.value ?? null) === null) continue;
    const now = ownValue(row.current, field);
    changes.push({
      field,
      label: FIELD_LABEL[field],
      current:
        row.current === null
          ? null
          : now.has
            ? formatValue(field, now.value)
            : UNKNOWN_TEXT,
      requested:
        asked.has && asked.value != null
          ? formatValue(field, asked.value)
          : MISSING_TEXT,
      isGate: isGateField(row.kind, field),
      note: noteFor(row.kind, field, context),
      isUnknown: false,
    });
  }

  for (const [field, value] of Object.entries(row.payload)) {
    if (isKnownField(row.kind, field) || value == null) continue;
    const now = ownValue(row.current, field);
    changes.push({
      field: safeText(field, MAX_VALUE_CHARACTERS),
      label: safeText(field, MAX_VALUE_CHARACTERS),
      current:
        row.current === null
          ? null
          : now.has
            ? formatValue(field, now.value)
            : UNKNOWN_TEXT,
      requested: formatValue(field, value),
      isGate: false,
      note: "This page does not know this field; read it before approving.",
      isUnknown: true,
    });
  }
  return changes;
}

function appTextOf(row: RequestRow): string {
  if (row.app_slug !== null)
    return safeText(row.app_slug, MAX_VALUE_CHARACTERS);
  const slug = row.payload.slug;
  return typeof slug === "string"
    ? `${safeText(slug, MAX_VALUE_CHARACTERS)} (new)`
    : "(new app)";
}

function sameValue(first: unknown, second: unknown): boolean {
  return JSON.stringify(first) === JSON.stringify(second);
}

function buildWarnings(row: RequestRow, context: InboxContext): string[] {
  const warnings: string[] = [];
  const slug = row.payload.slug;

  if (
    typeof slug === "string" &&
    context.apps.some((app) => app.slug === slug && app.slug !== row.app_slug)
  ) {
    warnings.push(
      `An app with the slug '${safeText(slug, MAX_VALUE_CHARACTERS)}' already exists. Approving closes this request as denied (automatic).`,
    );
  }

  if (row.kind === "delegation" && row.payload.shared_sp === true) {
    if (!context.isSharedSpAllowed) {
      warnings.push(
        "Approval will fail while the shared_sp_delegation_allowed setting is off. Turn it on under Settings, or deny the request.",
      );
    }
  }

  const budget =
    row.kind === "register"
      ? context.defaultMemoryBudgetMb
      : row.payload.memory_budget_mb;
  if (typeof budget === "number") {
    const currentBudget = row.current?.memory_budget_mb;
    const needed =
      typeof currentBudget === "number" ? budget - currentBudget : budget;
    if (needed > context.freeMemoryMb) {
      warnings.push(
        typeof currentBudget === "number"
          ? `The budget grows by ${formatMegabytes(needed)}, more than the ${formatMegabytes(Math.max(context.freeMemoryMb, 0))} free in the slot now; admission control may refuse to start the app.`
          : `The budget of ${formatMegabytes(budget)} is larger than the ${formatMegabytes(Math.max(context.freeMemoryMb, 0))} free in the slot now; admission control may refuse to start the app.`,
      );
    }
  }

  if (row.kind !== "register" && row.current !== null) {
    const asked = Object.entries(row.payload).filter(
      ([, value]) => value != null,
    );
    if (
      asked.length > 0 &&
      asked.every(([key, value]) => {
        const now = ownValue(row.current, key);
        return now.has && sameValue(now.value, value);
      })
    ) {
      warnings.push(
        "Every requested value is already set; approving only closes the request.",
      );
    }
  }
  return warnings;
}

export function inboxItem(row: RequestRow, context: InboxContext): InboxItem {
  return {
    id: row.request_id,
    kind: row.kind,
    kindLabel: KIND_LABEL[row.kind],
    appText: appTextOf(row),
    requestedBy: safeText(row.requested_by, MAX_VALUE_CHARACTERS),
    requestedAt: row.requested_at,
    reason: safeText(row.reason, MAX_REASON_CHARACTERS, {
      keepLineBreaks: true,
    }),
    changes: buildChanges(row, context),
    warnings: buildWarnings(row, context),
    requiresReason: row.requires_reason,
  };
}

// --------------------------------------------------------------- decisions

export type Decision = "approve" | "deny";

export interface DecisionValues {
  reason: string;
  note: string;
}

/**
 * The body of approve or deny. Blank fields are left out. Approving a request
 * that holds a gate field needs a reason (the server refuses without one);
 * denying never does.
 */
export function parseDecision(
  item: Pick<InboxItem, "requiresReason">,
  decision: Decision,
  values: DecisionValues,
): FormParse<DecidePermissionRequest> {
  const reason = values.reason.trim();
  const note = values.note.trim();
  if (decision === "approve" && item.requiresReason && reason === "") {
    return {
      ok: false,
      errors: { reason: "is required to approve this request" },
    };
  }
  const parsed = DecidePermissionRequest.safeParse({
    ...(reason === "" ? {} : { reason }),
    ...(note === "" ? {} : { note }),
  });
  return parsed.success
    ? { ok: true, value: parsed.data }
    : { ok: false, errors: formErrors(parsed.error.issues) };
}

/** What to tell the admin after the server answered with `row`. An automatic denial is not an approval. */
export function describeDecision(
  decision: Decision,
  row: RequestRow,
): ActionOutcome {
  const id = row.request_id;
  const where = `${KIND_LABEL[row.kind]}: ${appTextOf(row)}`;
  if (row.status === "approved" && decision === "approve") {
    return { tone: "success", title: `Approved request #${id}`, detail: where };
  }
  if (row.status === "denied" && decision === "deny") {
    return { tone: "info", title: `Denied request #${id}`, detail: where };
  }
  if (row.status === "denied") {
    return {
      tone: "warning",
      title: `Request #${id} was closed as denied, not approved`,
      detail:
        row.closed_note === null
          ? where
          : safeText(row.closed_note, MAX_REASON_CHARACTERS),
    };
  }
  return {
    tone: "warning",
    title: `Request #${id} is now ${row.status}`,
    detail: where,
  };
}
