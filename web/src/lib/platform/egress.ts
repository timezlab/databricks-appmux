/**
 * The egress rule editor's state and checks (design 6.3, admin-api
 * `PUT /platform/egress` and `PUT /apps/{app}/egress`).
 *
 * The rule list is one ordered value that a save replaces whole, and other
 * admins can change it while it is open, so the editor keeps a draft next to
 * the rules it started from (`base`):
 *
 * - while the draft equals `base`, nothing is being edited and a newer poll is
 *   simply shown;
 * - while the draft differs from `base` and the stored rules change under it,
 *   the draft is kept (the person's typing is not thrown away), the page says
 *   so, and Save is blocked until the person discards;
 * - a poll older than a save the page already made (lower `version`) is ignored.
 *
 * `syncEgressDraft` is pure and takes the latest polled rules, so the page
 * derives what it shows in render with no effect, and every action carries the
 * same `source` the click was rendered from. The reducer therefore acts on the
 * rule ids the person saw.
 *
 * The warnings are advice from design 6.3 (the default list, a closing deny,
 * first match wins); they never block a save. The rules a client may not send
 * (an allow over `@container_ip`, `@appmux_port` or 169.254.0.0/16) are
 * refused by `EgressRuleRequest`.
 */
import {
  ANY_PORT,
  CONTAINER_IP_PLACEHOLDER,
  EgressRuleRequest,
  LINK_LOCAL_RANGE,
} from "@/lib/api/schemas";
import type { EgressRule } from "@/lib/api/schemas";
import { formErrors } from "./forms";
import type { FormErrors } from "./forms";

const ANY_ADDRESS = "0.0.0.0/0";

type RuleFields = Pick<EgressRule, "action" | "cidr" | "port" | "comment">;

export interface EgressDraftRule extends RuleFields {
  /** Stable while the rule is on screen (a React key, and how a click names its rule). */
  id: number;
}

/** The stored rules as last polled, and the `apps` version they came with (`null` for the global list, which has none). */
export interface EgressSource {
  rules: readonly EgressRule[];
  version: number | null;
}

export interface EgressDraftState {
  rules: EgressDraftRule[];
  /** The stored rules the draft started from. */
  base: EgressRule[];
  version: number | null;
  nextId: number;
}

function fieldsOf(rule: RuleFields): RuleFields {
  return {
    action: rule.action,
    cidr: rule.cidr,
    port: rule.port,
    comment: rule.comment,
  };
}

function trimmed(rule: RuleFields): RuleFields {
  return {
    action: rule.action,
    cidr: rule.cidr.trim(),
    port: rule.port.trim(),
    comment: rule.comment.trim(),
  };
}

function sameRules(
  first: readonly RuleFields[],
  second: readonly RuleFields[],
): boolean {
  return (
    first.length === second.length &&
    first.every((rule, index) => {
      const other = second[index];
      return (
        other !== undefined &&
        JSON.stringify(trimmed(rule)) === JSON.stringify(trimmed(other))
      );
    })
  );
}

export function createEgressDraft(
  source: EgressSource,
  firstId = 1,
): EgressDraftState {
  return {
    rules: source.rules.map((rule, index) => ({
      id: firstId + index,
      ...fieldsOf(rule),
    })),
    base: source.rules.map(fieldsOf),
    version: source.version,
    nextId: firstId + source.rules.length,
  };
}

export function isEgressDraftDirty(state: EgressDraftState): boolean {
  return !sameRules(state.rules, state.base);
}

function isStale(state: EgressDraftState, source: EgressSource): boolean {
  return (
    state.version !== null &&
    source.version !== null &&
    source.version < state.version
  );
}

/** The state the page shows given the latest poll; see the module comment for the four cases. */
export function syncEgressDraft(
  state: EgressDraftState,
  source: EgressSource,
): EgressDraftState {
  if (isStale(state, source)) return state;
  if (sameRules(source.rules, state.base)) {
    return source.version === state.version
      ? state
      : { ...state, version: source.version };
  }
  if (!isEgressDraftDirty(state))
    return createEgressDraft(source, state.nextId);
  if (sameRules(state.rules, source.rules)) {
    return {
      ...state,
      base: source.rules.map(fieldsOf),
      version: source.version,
    };
  }
  return state;
}

/** Another admin changed the stored rules while this draft was being edited: Save must wait for Discard. */
export function hasRemoteEgressChange(
  state: EgressDraftState,
  source: EgressSource,
): boolean {
  return !isStale(state, source) && !sameRules(source.rules, state.base);
}

// ----------------------------------------------------------------- reducer

export type EgressRuleField = "action" | "cidr" | "port" | "comment";

export type EgressDraftAction =
  | { type: "add"; source: EgressSource }
  | { type: "remove"; id: number; source: EgressSource }
  | {
      type: "move";
      id: number;
      direction: "up" | "down";
      source: EgressSource;
    }
  | {
      type: "edit";
      id: number;
      field: EgressRuleField;
      value: string;
      source: EgressSource;
    }
  | { type: "discard"; source: EgressSource }
  /** The server stored these rules (the answer of the PUT). */
  | { type: "saved"; rules: readonly EgressRule[]; version: number | null };

function moved(
  rules: EgressDraftRule[],
  id: number,
  direction: "up" | "down",
): EgressDraftRule[] {
  const index = rules.findIndex((rule) => rule.id === id);
  const other = direction === "up" ? index - 1 : index + 1;
  const first = rules[Math.min(index, other)];
  const second = rules[Math.max(index, other)];
  if (index < 0 || first === undefined || second === undefined) return rules;
  return rules.toSpliced(Math.min(index, other), 2, second, first);
}

export function egressDraftReducer(
  state: EgressDraftState,
  action: EgressDraftAction,
): EgressDraftState {
  if (action.type === "saved") {
    return createEgressDraft(
      { rules: action.rules, version: action.version },
      state.nextId,
    );
  }

  const synced = syncEgressDraft(state, action.source);
  switch (action.type) {
    case "add":
      return {
        ...synced,
        rules: [
          ...synced.rules,
          {
            id: synced.nextId,
            action: "deny",
            cidr: "",
            port: ANY_PORT,
            comment: "",
          },
        ],
        nextId: synced.nextId + 1,
      };
    case "remove":
      return {
        ...synced,
        rules: synced.rules.filter((rule) => rule.id !== action.id),
      };
    case "move":
      return {
        ...synced,
        rules: moved(synced.rules, action.id, action.direction),
      };
    case "edit":
      return {
        ...synced,
        rules: synced.rules.map((rule) =>
          rule.id === action.id
            ? { ...rule, [action.field]: action.value }
            : rule,
        ),
      };
    case "discard":
      return createEgressDraft(
        isStale(synced, action.source)
          ? { rules: synced.base, version: synced.version }
          : action.source,
        synced.nextId,
      );
  }
}

// -------------------------------------------------------------- validation

export type EgressValidation =
  | { ok: true; rules: EgressRuleRequest[] }
  /** The messages of each failing rule, by the rule's id (as text). */
  | { ok: false; errors: Record<string, FormErrors> };

/** Every rule through `EgressRuleRequest`, on its trimmed text. An empty list is valid: it clears the rules. */
export function validateEgressDraft(
  rules: readonly EgressDraftRule[],
): EgressValidation {
  const valid: EgressRuleRequest[] = [];
  const errors: Record<string, FormErrors> = {};
  for (const rule of rules) {
    const parsed = EgressRuleRequest.safeParse(trimmed(rule));
    if (parsed.success) valid.push(parsed.data);
    else errors[String(rule.id)] = formErrors(parsed.error.issues);
  }
  return Object.keys(errors).length === 0
    ? { ok: true, rules: valid }
    : { ok: false, errors };
}

// ---------------------------------------------------------------- warnings

function isDenyAll(rule: RuleFields): boolean {
  return (
    rule.action === "deny" &&
    rule.cidr === ANY_ADDRESS &&
    rule.port === ANY_PORT
  );
}

/** `later` can never be the first match because `earlier` already matches all of its traffic. */
function isShadowedBy(later: RuleFields, earlier: RuleFields): boolean {
  if (later.cidr === "") return false;
  if (earlier.cidr === later.cidr && earlier.port === later.port) return true;
  return (
    earlier.port === ANY_PORT &&
    (earlier.cidr === ANY_ADDRESS || earlier.cidr === later.cidr)
  );
}

/**
 * Advice for the rules as typed. `scope` is the global list (the default list
 * blocks the container's own address and the metadata range, design 6.3) or one
 * app's list (an allow-list ends with a deny of everything).
 */
export function egressWarnings(
  rules: readonly RuleFields[],
  scope: "global" | "app",
): string[] {
  const list = rules.map(trimmed);
  const warnings: string[] = [];

  if (scope === "global") {
    const blocks = (cidr: (value: string) => boolean) =>
      list.some(
        (rule) =>
          isDenyAll(rule) ||
          (rule.action === "deny" && rule.port === ANY_PORT && cidr(rule.cidr)),
      );
    if (!blocks((cidr) => cidr.startsWith(CONTAINER_IP_PLACEHOLDER))) {
      warnings.push(
        `Nothing denies ${CONTAINER_IP_PLACEHOLDER} (for example 'deny ${CONTAINER_IP_PLACEHOLDER}/32' on any port): a child could reach every service listening on the container's own address.`,
      );
    }
    if (!blocks((cidr) => cidr === LINK_LOCAL_RANGE)) {
      warnings.push(
        `Nothing denies ${LINK_LOCAL_RANGE} on any port: a child could reach the metadata and link-local addresses.`,
      );
    }
  } else {
    const last = list.at(-1);
    if (
      list.some((rule) => rule.action === "allow") &&
      (last === undefined || !isDenyAll(last))
    ) {
      warnings.push(
        "This list allows some traffic but does not end with 'deny 0.0.0.0/0' on any port, so everything it does not mention is still allowed.",
      );
    }
  }

  list.forEach((rule, index) => {
    const decidedBy = list
      .slice(0, index)
      .findIndex((earlier) => isShadowedBy(rule, earlier));
    if (decidedBy >= 0) {
      warnings.push(
        `Rule ${index + 1} can never match: rule ${decidedBy + 1} already decides the same traffic (the first match wins).`,
      );
    }
  });
  return warnings;
}
