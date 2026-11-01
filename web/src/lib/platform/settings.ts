/**
 * The platform settings on the platform page (data model 4.1, admin-api
 * `PUT /platform/settings/{key}`): what each key means, its level of effect
 * (design premise 4: every one of these reaches all children), and the editor
 * that fits its type.
 *
 * `PutSettingRequest` takes any JSON for a value, so the contract names the
 * rules the page can check (a whole number, the two linked keys, the 290 s
 * ceiling, the package modes) and leaves every other limit to the server, whose
 * 400 the page shows as it is.
 */
import { PackageMode, PutSettingRequest } from "@/lib/api/schemas";
import type { PlatformView, SettingKey } from "@/lib/api/schemas";
import { formatCount } from "@/lib/format";
import { formErrors, parseWholeNumber } from "./forms";
import type { FormParse } from "./forms";

export type PlatformSettings = PlatformView["settings"];

/** The only level any of these keys has: they are not per app. */
export const ALL_CHILDREN = "All children";

/** `request_timeout_s` is at most this (data model 4.1: below the platform's 300 s). */
const MAX_REQUEST_TIMEOUT_S = 290;
/** `max_locked_releases <= max_releases_per_app - RELEASES_LEFT_UNLOCKED` (data model 4.1). */
const RELEASES_LEFT_UNLOCKED = 4;

export type SettingEditor =
  | { kind: "integer"; unit: "MB" | "s" | null }
  | { kind: "boolean" }
  | { kind: "choice"; options: readonly string[] }
  | { kind: "list" }
  | { kind: "optional-text" };

export interface SettingDescriptor {
  key: SettingKey;
  /** The key as the API names it. */
  label: string;
  meaning: string;
  levelOfEffect: typeof ALL_CHILDREN;
  editor: SettingEditor;
}

function describe(
  key: SettingKey,
  meaning: string,
  editor: SettingEditor,
): SettingDescriptor {
  return { key, label: key, meaning, levelOfEffect: ALL_CHILDREN, editor };
}

export const SETTING_DESCRIPTORS: Record<SettingKey, SettingDescriptor> = {
  memory_reserve_mb: describe(
    "memory_reserve_mb",
    "Memory kept for appmux itself. The apps share the rest of the container's limit.",
    { kind: "integer", unit: "MB" },
  ),
  package_mode: describe(
    "package_mode",
    "Where a build takes packages from: the index, only wheel files, or wheel files first and then the index.",
    { kind: "choice", options: PackageMode.options },
  ),
  default_wheel_sources: describe(
    "default_wheel_sources",
    "Folders every Update also reads wheel files from. One folder per line.",
    { kind: "list" },
  ),
  shared_sp_delegation_allowed: describe(
    "shared_sp_delegation_allowed",
    "Master switch. When off, no app is lent the appmux service principal's token and no request for it is accepted.",
    { kind: "boolean" },
  ),
  max_releases_per_app: describe(
    "max_releases_per_app",
    "Live releases kept per app.",
    { kind: "integer", unit: null },
  ),
  max_locked_releases: describe(
    "max_locked_releases",
    "Releases an app admin may lock by hand, per app. At most max_releases_per_app minus 4.",
    { kind: "integer", unit: null },
  ),
  request_timeout_s: describe(
    "request_timeout_s",
    "Per-request timeout the edge enforces on each app. At most 290, below the platform's 300 s.",
    { kind: "integer", unit: "s" },
  ),
  max_request_body_mb: describe(
    "max_request_body_mb",
    "Request body cap per app.",
    { kind: "integer", unit: "MB" },
  ),
  max_concurrent_requests: describe(
    "max_concurrent_requests",
    "Requests in flight per app before the edge answers 429.",
    { kind: "integer", unit: null },
  ),
  build_timeout_s: describe(
    "build_timeout_s",
    "Wall-clock limit of one build in the sandbox.",
    { kind: "integer", unit: "s" },
  ),
  max_processes_per_child: describe(
    "max_processes_per_child",
    "Process limit (RLIMIT_NPROC) set per sandbox.",
    { kind: "integer", unit: null },
  ),
  child_cpu_pin: describe(
    "child_cpu_pin",
    "Pin every sandbox to one CPU at nice 19 and refuse sched_setaffinity by seccomp.",
    { kind: "boolean" },
  ),
  attach_check_warehouse_id: describe(
    "attach_check_warehouse_id",
    "The SQL warehouse the attach checks of secret, warehouse and function resources use. Unset by default; an attach that needs it is refused naming this key.",
    { kind: "optional-text" },
  ),
};

// ----------------------------------------------------------------- reading

/** The current value as the text its editor starts with. */
export function settingDraft(
  key: SettingKey,
  settings: PlatformSettings,
): string {
  switch (key) {
    case "default_wheel_sources":
      return settings[key].join("\n");
    case "attach_check_warehouse_id":
      return settings[key] ?? "";
    case "package_mode":
      return settings[key];
    case "shared_sp_delegation_allowed":
    case "child_cpu_pin":
      return String(settings[key]);
    case "memory_reserve_mb":
    case "max_releases_per_app":
    case "max_locked_releases":
    case "request_timeout_s":
    case "max_request_body_mb":
    case "max_concurrent_requests":
    case "build_timeout_s":
    case "max_processes_per_child":
      return String(settings[key]);
  }
}

/** The current value in words, with its unit. */
export function formatSettingValue(
  key: SettingKey,
  settings: PlatformSettings,
): string {
  const editor = SETTING_DESCRIPTORS[key].editor;
  switch (editor.kind) {
    case "integer": {
      const count = formatCount(Number(settingDraft(key, settings)));
      return editor.unit === null ? count : `${count} ${editor.unit}`;
    }
    case "boolean":
      return settingDraft(key, settings) === "true" ? "On" : "Off";
    case "choice":
      return settingDraft(key, settings);
    case "list": {
      const lines = settingDraft(key, settings);
      return lines === "" ? "none" : lines.replaceAll("\n", ", ");
    }
    case "optional-text": {
      const text = settingDraft(key, settings);
      return text === "" ? "not set" : text;
    }
  }
}

// ----------------------------------------------------------------- writing

/** The rules of one key on its own: a whole number, the 290 s ceiling, the package modes. */
function ownProblem(key: SettingKey, value: number): string | null {
  if (key === "request_timeout_s" && value > MAX_REQUEST_TIMEOUT_S) {
    return `must be at most ${MAX_REQUEST_TIMEOUT_S}`;
  }
  return null;
}

/**
 * The rule between two keys (data model 4.1): `max_locked_releases` is at most
 * `max_releases_per_app - 4`. `others` holds the values the other key will have
 * when this one is saved: the stored ones, or the ones being entered beside it.
 */
export function settingCrossFieldProblem(
  key: SettingKey,
  value: unknown,
  others: PlatformSettings,
): string | null {
  if (typeof value !== "number") return null;
  if (key === "max_locked_releases") {
    const most = others.max_releases_per_app - RELEASES_LEFT_UNLOCKED;
    if (value > most) {
      return `must be at most ${most} (max_releases_per_app is ${others.max_releases_per_app}, minus ${RELEASES_LEFT_UNLOCKED})`;
    }
  }
  if (key === "max_releases_per_app") {
    const least = others.max_locked_releases + RELEASES_LEFT_UNLOCKED;
    if (value < least) {
      return `must be at least ${least} (max_locked_releases is ${others.max_locked_releases}, plus ${RELEASES_LEFT_UNLOCKED})`;
    }
  }
  return null;
}

function fail(message: string): { ok: false; errors: { value: string } } {
  return { ok: false, errors: { value: message } };
}

/**
 * The body of `PUT /platform/settings/{key}` for the text in the editor, by the
 * rules of the key alone (the rule between two keys is `settingCrossFieldProblem`).
 * Every error is filed under `value`.
 */
export function parseSettingValue(
  key: SettingKey,
  draft: string,
): FormParse<PutSettingRequest> {
  const editor = SETTING_DESCRIPTORS[key].editor;
  let value: unknown;
  switch (editor.kind) {
    case "integer": {
      const number = parseWholeNumber(draft);
      if (number === null) {
        return fail(
          draft.trim() === "" ? "is required" : "must be a whole number",
        );
      }
      const problem = ownProblem(key, number);
      if (problem !== null) return fail(problem);
      value = number;
      break;
    }
    case "boolean":
      value = draft === "true";
      break;
    case "choice":
      if (!editor.options.includes(draft)) {
        return fail(`must be one of ${editor.options.join(", ")}`);
      }
      value = draft;
      break;
    case "list":
      value = draft
        .split(/\r?\n/)
        .map((line) => line.trim())
        .filter((line) => line !== "");
      break;
    case "optional-text": {
      const text = draft.trim();
      value = text === "" ? null : text;
      break;
    }
  }
  const parsed = PutSettingRequest.safeParse({ value });
  return parsed.success
    ? { ok: true, value: parsed.data }
    : { ok: false, errors: formErrors(parsed.error.issues) };
}

/** `parseSettingValue` plus the rule between two keys, checked against `settings`. */
export function parseSettingDraft(
  key: SettingKey,
  draft: string,
  settings: PlatformSettings,
): FormParse<PutSettingRequest> {
  const parsed = parseSettingValue(key, draft);
  if (!parsed.ok) return parsed;
  const problem = settingCrossFieldProblem(key, parsed.value.value, settings);
  return problem === null ? parsed : fail(problem);
}

/** Whether saving `draft` would change anything; text that is not valid yet counts as a change. */
export function isSettingChanged(
  key: SettingKey,
  draft: string,
  settings: PlatformSettings,
): boolean {
  const parsed = parseSettingValue(key, draft);
  if (!parsed.ok) return draft !== settingDraft(key, settings);
  const current = JSON.stringify(settings[key]);
  return JSON.stringify(parsed.value.value) !== current;
}

/** A sentence to confirm before saving this value, or `null` when saving needs no second look. */
export function settingSaveWarning(
  key: SettingKey,
  value: unknown,
): string | null {
  if (key === "shared_sp_delegation_allowed" && value === true) {
    return "Turning this on lets you lend the appmux service principal's token to apps. An app that has it can use everything that service principal can reach.";
  }
  return null;
}
