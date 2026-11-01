/**
 * The edits on the settings page, kept apart from the stored values until the
 * person saves them all at once (design 10.1: one save bar).
 *
 * The API takes one setting per call (`PUT /platform/settings/{key}`, no batch
 * and no per-key version), so "Save changes" is a sequence of calls and any of
 * them can fail on its own. Everything here is pure: what changed, what is
 * wrong, in which order to send, what came of it, and how the edits survive a
 * new view arriving from the poll.
 */
import type { PutSettingRequest, SettingKey } from "@/lib/api/schemas";
import { pluralize } from "@/lib/format";
import {
  isSettingChanged,
  parseSettingValue,
  settingCrossFieldProblem,
  settingDraft,
} from "./settings";
import type { PlatformSettings } from "./settings";
import { SETTING_GROUPS } from "./sections";

/** The text each edited setting holds in its editor; a key is present only while the person has typed in it. */
export type SettingDrafts = Partial<Record<SettingKey, string>>;
export type SettingErrors = Partial<Record<SettingKey, string>>;
/** Values by key, each of the type the contract gives that key. */
export type ConfirmedSettings = { [K in SettingKey]?: PlatformSettings[K] };

/** The settings in the order the page lists them: group by group. */
const PAGE_ORDER: readonly SettingKey[] = SETTING_GROUPS.flatMap(
  (group) => group.keys,
);

function draftedKeys(drafts: SettingDrafts): SettingKey[] {
  return PAGE_ORDER.filter((key) => drafts[key] !== undefined);
}

/** The drafted keys whose text differs from the stored value, in page order. */
export function changedSettingKeys(
  drafts: SettingDrafts,
  stored: PlatformSettings,
): SettingKey[] {
  return draftedKeys(drafts).filter((key) =>
    isSettingChanged(key, drafts[key] ?? "", stored),
  );
}

/**
 * The settings as they will be once every valid draft is saved: the stored
 * values with the drafts laid over them. A draft that is not valid on its own
 * leaves the stored value in place.
 */
export function effectiveSettings(
  drafts: SettingDrafts,
  stored: PlatformSettings,
): PlatformSettings {
  const effective: Record<string, unknown> = { ...stored };
  for (const key of changedSettingKeys(drafts, stored)) {
    const parsed = parseSettingValue(key, drafts[key] ?? "");
    if (parsed.ok) effective[key] = parsed.value.value;
  }
  // Each key holds the type the contract gives it: the parse of that key produced it.
  return effective as PlatformSettings;
}

/**
 * What is wrong with the changed settings, by key. The rule between the two
 * release keys is checked against the values being entered beside it, so
 * raising the cap and the locks together is valid even though the locks alone
 * would not be.
 */
export function settingErrors(
  drafts: SettingDrafts,
  stored: PlatformSettings,
): SettingErrors {
  const effective = effectiveSettings(drafts, stored);
  const errors: SettingErrors = {};
  for (const key of changedSettingKeys(drafts, stored)) {
    const parsed = parseSettingValue(key, drafts[key] ?? "");
    if (!parsed.ok) {
      errors[key] = parsed.errors.value;
      continue;
    }
    const problem = settingCrossFieldProblem(
      key,
      parsed.value.value,
      effective,
    );
    if (problem !== null) errors[key] = problem;
  }
  return errors;
}

export interface SettingSaveStep {
  key: SettingKey;
  body: PutSettingRequest;
}

export type SettingsSavePlan =
  { ok: true; steps: SettingSaveStep[] } | { ok: false; errors: SettingErrors };

/**
 * `max_locked_releases <= max_releases_per_app - 4` is checked by the server on
 * every call against what is stored at that moment. Raising the cap first and
 * lowering the cap last keeps each call valid on its own: when the cap goes
 * down, the locks (which then have to be lower than the new cap allows) go
 * first.
 */
function orderForServer(
  keys: SettingKey[],
  stored: PlatformSettings,
  effective: PlatformSettings,
): SettingKey[] {
  const capAt = keys.indexOf("max_releases_per_app");
  const locksAt = keys.indexOf("max_locked_releases");
  const isLoweringCap =
    capAt !== -1 &&
    locksAt !== -1 &&
    effective.max_releases_per_app < stored.max_releases_per_app;
  if (!isLoweringCap || locksAt < capAt) return keys;
  const ordered = [...keys];
  ordered[capAt] = "max_locked_releases";
  ordered[locksAt] = "max_releases_per_app";
  return ordered;
}

/**
 * The calls to make for the drafts: one body per changed setting, in the order
 * that keeps every call valid. Nothing is planned while any row is wrong, so a
 * save never sends half of a set of changes the person has not yet fixed.
 */
export function planSettingsSave(
  drafts: SettingDrafts,
  stored: PlatformSettings,
): SettingsSavePlan {
  const errors = settingErrors(drafts, stored);
  if (Object.keys(errors).length > 0) return { ok: false, errors };
  const effective = effectiveSettings(drafts, stored);
  const keys = orderForServer(
    changedSettingKeys(drafts, stored),
    stored,
    effective,
  );
  const steps: SettingSaveStep[] = [];
  for (const key of keys) {
    const parsed = parseSettingValue(key, drafts[key] ?? "");
    if (parsed.ok) steps.push({ key, body: parsed.value });
  }
  return { ok: true, steps };
}

// ------------------------------------------------------------------ saving

export type SaveStepResult =
  | { key: SettingKey; status: "saved" }
  | { key: SettingKey; status: "failed"; error: unknown }
  | { key: SettingKey; status: "skipped"; because: SettingKey };

/**
 * Sends the steps one after the other (never two at once: the server checks the
 * rule between two keys against what is stored when each call arrives) and
 * reports each one. A failure does not stop the rest, except that the second
 * release key is not sent when the first failed: the server would refuse it
 * against the old value.
 */
export async function runSettingsSave(
  steps: readonly SettingSaveStep[],
  put: (key: SettingKey, body: PutSettingRequest) => Promise<unknown>,
): Promise<SaveStepResult[]> {
  const results: SaveStepResult[] = [];
  const failed = new Set<SettingKey>();
  for (const step of steps) {
    const dependsOn = RELEASE_PAIR.includes(step.key)
      ? RELEASE_PAIR.find(
          (partner) => partner !== step.key && failed.has(partner),
        )
      : undefined;
    if (dependsOn !== undefined) {
      results.push({ key: step.key, status: "skipped", because: dependsOn });
      continue;
    }
    try {
      await put(step.key, step.body);
      results.push({ key: step.key, status: "saved" });
    } catch (error) {
      failed.add(step.key);
      results.push({ key: step.key, status: "failed", error });
    }
  }
  return results;
}

const RELEASE_PAIR: readonly SettingKey[] = [
  "max_releases_per_app",
  "max_locked_releases",
];

/**
 * The values the server accepted, by key, for the steps that were saved. Each
 * body was produced by the parse of its own key, so it holds the type the
 * contract gives that key.
 */
export function savedValues(
  steps: readonly SettingSaveStep[],
  results: readonly SaveStepResult[],
): ConfirmedSettings {
  const saved: Record<string, unknown> = {};
  for (const step of steps) {
    const result = results.find((candidate) => candidate.key === step.key);
    if (result?.status === "saved") saved[step.key] = step.body.value;
  }
  return saved;
}

/** "Saved 3 settings", or "2 of 3 saved, 1 failed" (and "1 not sent") when some were not. */
export function saveSummary(results: readonly SaveStepResult[]): string {
  const saved = results.filter((result) => result.status === "saved").length;
  const failed = results.filter((result) => result.status === "failed").length;
  const skipped = results.filter(
    (result) => result.status === "skipped",
  ).length;
  if (failed === 0 && skipped === 0) {
    return `Saved ${pluralize(saved, "setting")}`;
  }
  const parts = [`${saved} of ${results.length} saved`];
  if (failed > 0) parts.push(`${failed} failed`);
  if (skipped > 0) parts.push(`${skipped} not sent`);
  return parts.join(", ");
}

// ----------------------------------------------------------------- reducer

export interface SettingsDraftState {
  drafts: SettingDrafts;
  /** The stored text each draft started from, to notice that another admin changed it meanwhile. */
  bases: SettingDrafts;
  /** What the server accepted that the latest view has not shown yet. */
  confirmed: ConfirmedSettings;
  /** A sentence per row whose last save did not go through. */
  failures: SettingErrors;
  /** The stored text now, for a row another admin changed while the person was editing it. */
  conflicts: SettingDrafts;
  /** How the last save went ("2 of 3 saved, 1 failed"); `null` before a save and after any further edit. */
  lastSave: string | null;
}

export const INITIAL_SETTINGS_DRAFT: SettingsDraftState = {
  drafts: {},
  bases: {},
  confirmed: {},
  failures: {},
  conflicts: {},
  lastSave: null,
};

export type SettingsDraftEvent =
  | {
      type: "edited";
      key: SettingKey;
      text: string;
      /** The stored values, with what the server accepted but the view lacks. */
      stored: PlatformSettings;
    }
  | { type: "discarded" }
  | {
      type: "saveFinished";
      results: readonly SaveStepResult[];
      /** The value of each saved key, as sent. */
      saved: ConfirmedSettings;
      /** A sentence for each failed or skipped key. */
      failures: SettingErrors;
    }
  | { type: "viewChanged"; stored: PlatformSettings };

function without<T>(
  record: Partial<Record<SettingKey, T>>,
  key: SettingKey,
): Partial<Record<SettingKey, T>> {
  const rest = { ...record };
  delete rest[key];
  return rest;
}

export function settingsDraftReducer(
  state: SettingsDraftState,
  event: SettingsDraftEvent,
): SettingsDraftState {
  switch (event.type) {
    case "edited": {
      const { key, text, stored } = event;
      if (text === settingDraft(key, stored)) {
        return {
          ...state,
          drafts: without(state.drafts, key),
          bases: without(state.bases, key),
          failures: without(state.failures, key),
          conflicts: without(state.conflicts, key),
          lastSave: null,
        };
      }
      return {
        ...state,
        drafts: { ...state.drafts, [key]: text },
        bases: {
          ...state.bases,
          [key]: state.bases[key] ?? settingDraft(key, stored),
        },
        failures: without(state.failures, key),
        lastSave: null,
      };
    }
    case "discarded":
      return {
        ...INITIAL_SETTINGS_DRAFT,
        confirmed: state.confirmed,
      };
    case "saveFinished": {
      let next = state;
      for (const result of event.results) {
        if (result.status !== "saved") continue;
        next = {
          ...next,
          drafts: without(next.drafts, result.key),
          bases: without(next.bases, result.key),
          failures: without(next.failures, result.key),
          conflicts: without(next.conflicts, result.key),
        };
      }
      return {
        ...next,
        confirmed: { ...next.confirmed, ...event.saved },
        failures: { ...next.failures, ...event.failures },
        lastSave: saveSummary(event.results),
      };
    }
    case "viewChanged": {
      let drafts = state.drafts;
      let bases = state.bases;
      let conflicts = state.conflicts;
      for (const key of draftedKeys(state.drafts)) {
        const storedText = settingDraft(key, event.stored);
        if (storedText === state.bases[key]) continue;
        if (state.drafts[key] === storedText) {
          drafts = without(drafts, key);
          bases = without(bases, key);
          conflicts = without(conflicts, key);
        } else {
          bases = { ...bases, [key]: storedText };
          conflicts = { ...conflicts, [key]: storedText };
        }
      }
      const hasConfirmed = Object.keys(state.confirmed).length > 0;
      if (
        drafts === state.drafts &&
        bases === state.bases &&
        conflicts === state.conflicts &&
        !hasConfirmed
      ) {
        return state;
      }
      return { ...state, drafts, bases, conflicts, confirmed: {} };
    }
  }
}
