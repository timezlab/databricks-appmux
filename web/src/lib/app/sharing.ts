/**
 * Who may open and manage the app: its grants (design 9.1). The owner is not a
 * grant row; the page shows them as a fixed first row. A platform admin holds
 * `CAN_MANAGE` implicitly and is not listed, because there is no row to show.
 */
import { GrantRequest, PrincipalQuery } from "@/lib/api";
import type {
  AppDetail,
  AppPermission,
  GrantType,
  PrincipalList,
  PrincipalSuggestion,
} from "@/lib/api";
import { REASON_NOT_MANAGER, REASON_UNKNOWN_ACTOR } from "./actions";
import type { Actor, Availability } from "./actions";
import { errorsFromZod, formFailure } from "./forms";
import type { FormErrors, FormResult } from "./forms";

export interface GrantRow {
  /** Unique within the list, for the React key. */
  id: string;
  type: GrantType;
  name: string;
  permission: AppPermission;
  /** `null` for the owner, who is not a grant. */
  grantedBy: string | null;
  grantedAt: string | null;
  isOwner: boolean;
}

const EVERYONE_NAME = "*";

export function permissionLabel(permission: AppPermission): string {
  return permission === "CAN_MANAGE" ? "Can manage" : "Can use";
}

export function grantRows(app: AppDetail): GrantRow[] {
  const ownerEmail = app.owner.toLowerCase();
  const ownerRow: GrantRow = {
    id: `owner:${app.owner}`,
    type: "user",
    name: app.owner,
    permission: "CAN_MANAGE",
    grantedBy: null,
    grantedAt: null,
    isOwner: true,
  };
  const grantRowsInOrder = app.grants
    .filter(
      (grant) =>
        !(grant.type === "user" && grant.name.toLowerCase() === ownerEmail),
    )
    .map((grant): GrantRow => ({
      id: `${grant.type}:${grant.name}`,
      type: grant.type,
      name: grant.name,
      permission: grant.permission,
      grantedBy: grant.granted_by,
      grantedAt: grant.granted_at,
      isOwner: false,
    }));
  return [ownerRow, ...grantRowsInOrder];
}

// --------------------------------------------------------------- principals

export const PRINCIPAL_TYPE_LABEL: Readonly<Record<GrantType, string>> = {
  user: "User",
  group: "Group",
  service_principal: "Service principal",
  everyone: "Everyone",
};

/** The same type as a noun phrase, for "Use “x” as a group". */
const PRINCIPAL_TYPE_PHRASE: Readonly<Record<GrantType, string>> = {
  user: "a user",
  group: "a group",
  service_principal: "a service principal",
  everyone: "everyone",
};

// ADR 0020: a service principal is named by its application id, a UUID.
const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * What a typed text names, when no suggestion is picked: an `@` is a user's
 * e-mail, a UUID a service principal's application id, `everyone` or `*` is
 * everyone, anything else a group name. `null` for blank text.
 */
export function inferPrincipalType(text: string): GrantType | null {
  const trimmed = text.trim();
  if (trimmed === "") return null;
  if (trimmed === EVERYONE_NAME || trimmed.toLowerCase() === "everyone") {
    return "everyone";
  }
  if (trimmed.includes("@")) return "user";
  if (UUID_PATTERN.test(trimmed)) return "service_principal";
  return "group";
}

/** One principal the grant form can hold: picked from the suggestions, or typed. */
export interface PrincipalChoice {
  type: GrantType;
  /** What `GrantRequest.name` takes. */
  name: string;
  /** The name a person knows it by. */
  label: string;
  /** Shown in mono beside the label; `null` when it would repeat the label. */
  identifier: string | null;
}

/** The typed text as a principal, with the name the grant carries; `null` for blank text. */
export function typedPrincipal(text: string): PrincipalChoice | null {
  const type = inferPrincipalType(text);
  if (type === null) return null;
  const trimmed = text.trim();
  if (type === "everyone") {
    return {
      type,
      name: EVERYONE_NAME,
      label: PRINCIPAL_TYPE_LABEL.everyone,
      identifier: null,
    };
  }
  // The contract wants an e-mail and an application id in lower case; a
  // group name is matched as the directory spells it.
  const name = type === "group" ? trimmed : trimmed.toLowerCase();
  return { type, name, label: name, identifier: null };
}

function suggestionChoice(suggestion: PrincipalSuggestion): PrincipalChoice {
  const label = suggestion.display_name ?? suggestion.name;
  const identifier =
    suggestion.type === "everyone"
      ? null
      : (suggestion.detail ?? suggestion.name);
  return {
    type: suggestion.type,
    name: suggestion.name,
    label,
    identifier: identifier === label ? null : identifier,
  };
}

function isSamePrincipal(a: PrincipalChoice, b: PrincipalChoice): boolean {
  return a.type === b.type && a.name.toLowerCase() === b.name.toLowerCase();
}

export interface PrincipalRow {
  /** Unique within the list, for the React key and the list's value. */
  id: string;
  choice: PrincipalChoice;
  /** The "Use what I typed" row. */
  isTyped: boolean;
  /** The row's text for a screen reader and for the typed row's words. */
  text: string;
}

/**
 * The rows of the principal field: the suggestions in the server's order
 * (everyone, groups, users, service principals), then the typed text as a
 * "Use" row with its inferred type, unless a suggestion is that principal.
 */
export function principalRows(
  suggestions: readonly PrincipalSuggestion[],
  typed: string,
): PrincipalRow[] {
  const rows = suggestions.map((suggestion): PrincipalRow => {
    const choice = suggestionChoice(suggestion);
    return {
      id: `${choice.type}:${choice.name}`,
      choice,
      isTyped: false,
      text: [choice.label, choice.identifier, PRINCIPAL_TYPE_LABEL[choice.type]]
        .filter((part) => part !== null)
        .join(", "),
    };
  });
  const typedChoice = typedPrincipal(typed);
  if (
    typedChoice === null ||
    rows.some((row) => isSamePrincipal(row.choice, typedChoice))
  ) {
    return rows;
  }
  return [
    ...rows,
    {
      id: `typed:${typedChoice.type}:${typedChoice.name}`,
      choice: typedChoice,
      isTyped: true,
      text: `Use “${typed.trim()}” as ${PRINCIPAL_TYPE_PHRASE[typedChoice.type]}`,
    },
  ];
}

const DIRECTORY_NAMES: readonly [keyof PrincipalList["sources"], string][] = [
  ["users", "users"],
  ["groups", "groups"],
  ["service_principals", "service principals"],
];

function joinWords(words: readonly string[]): string {
  if (words.length <= 1) return words.join("");
  return `${words.slice(0, -1).join(", ")} and ${words.at(-1) ?? ""}`;
}

/**
 * The quiet note under the suggestions: which directories could not be read
 * (`sources`), or that the call failed (`null`). Typing in full still works,
 * so the note says so. `null` when every directory asked was read.
 */
export function principalSuggestionsNote(
  sources: PrincipalList["sources"] | null,
): string | null {
  if (sources === null) {
    return "Suggestions are unavailable. Type the full e-mail address, group name or service principal id, then use it.";
  }
  const unavailable = DIRECTORY_NAMES.filter(
    ([key]) => sources[key] === "unavailable",
  ).map(([, words]) => words);
  if (unavailable.length === 0) return null;
  return `Suggestions for ${joinWords(unavailable)} are unavailable here; type them in full, then use what you typed.`;
}

/**
 * Whether the server can search for `text` (`PrincipalQuery.q`: at most 64
 * letters, digits, spaces and `. _ @ + -`). Other text, such as `*`, is not
 * sent; it can still be used as typed.
 */
export function isSearchablePrincipalText(text: string): boolean {
  return PrincipalQuery.safeParse({ q: text }).success;
}

// -------------------------------------------------------------------- form

export interface LevelChoice {
  value: AppPermission;
  label: string;
  isAvailable: boolean;
  /** Why it cannot be chosen; `null` when it can. */
  reason: string | null;
}

/** Why `everyone` and a service principal stop at Can use (ADR 0020). */
const CAN_USE_ONLY_REASON: Partial<Record<GrantType, string>> = {
  everyone:
    "Everyone can only use the app; managing it needs a named user or group.",
  service_principal:
    "A service principal can only use the app, so every change to it has a person behind it (ADR 0020).",
};

/** The two levels, with Can manage disabled for a principal that cannot hold it. */
export function grantLevelChoices(type: GrantType | null): LevelChoice[] {
  const reason = type === null ? undefined : CAN_USE_ONLY_REASON[type];
  return [
    {
      value: "CAN_USE",
      label: permissionLabel("CAN_USE"),
      isAvailable: true,
      reason: null,
    },
    {
      value: "CAN_MANAGE",
      label: permissionLabel("CAN_MANAGE"),
      isAvailable: reason === undefined,
      reason: reason ?? null,
    },
  ];
}

/** What the add row holds: the principal (picked or typed) and the level. */
export interface GrantFormValues {
  principal: Pick<PrincipalChoice, "type" | "name"> | null;
  permission: AppPermission;
}

export const EMPTY_GRANT_FORM: GrantFormValues = {
  principal: null,
  permission: "CAN_USE",
};

/** A new principal keeps the level unless it cannot hold it. */
export function choosePrincipal(
  values: GrantFormValues,
  principal: GrantFormValues["principal"],
): GrantFormValues {
  const canManage =
    principal === null || CAN_USE_ONLY_REASON[principal.type] === undefined;
  return {
    principal,
    permission: canManage ? values.permission : "CAN_USE",
  };
}

export function buildGrantRequest(
  app: AppDetail,
  values: GrantFormValues,
): FormResult<GrantRequest> {
  const { principal, permission } = values;
  if (principal === null) {
    return formFailure({
      principal:
        "Choose who to share with: pick a suggestion, or type an e-mail address, a group name or a service principal id.",
    });
  }
  const errors: FormErrors = {};
  const { type } = principal;
  let name = principal.name.trim();

  const onlyUseReason = CAN_USE_ONLY_REASON[type];
  if (onlyUseReason !== undefined && permission !== "CAN_USE") {
    errors.permission = onlyUseReason;
  }
  if (type === "everyone") {
    name = EVERYONE_NAME;
  } else if (type === "user") {
    // The contract wants a lower-case email; people type capitals.
    name = name.toLowerCase();
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(name)) {
      errors.principal =
        "Type the full e-mail address, such as user@example.com.";
    } else if (
      name === app.owner.toLowerCase() &&
      permission !== "CAN_MANAGE"
    ) {
      errors.principal =
        "This is the app's owner, who always manages it; a lower level is refused.";
    }
  } else if (type === "service_principal") {
    name = name.toLowerCase();
    if (!UUID_PATTERN.test(name)) {
      errors.principal =
        "Use the service principal's application id, a UUID such as 4f3c1b2a-9d8e-4f7a-b6c5-0123456789ab.";
    }
  } else if (name === "") {
    errors.principal = "Type the name of a Databricks group.";
  }
  if (Object.keys(errors).length > 0) return formFailure(errors);

  const parsed = GrantRequest.safeParse({ type, name, permission });
  if (!parsed.success) {
    // The schema's fields are type, name and permission; the form shows
    // the first two on the principal field.
    const schemaErrors = errorsFromZod(parsed.error);
    const shown: FormErrors = {};
    for (const [field, message] of Object.entries(schemaErrors)) {
      shown[field === "permission" ? "permission" : "principal"] = message;
    }
    return formFailure(shown);
  }
  return { ok: true, body: parsed.data };
}

// ----------------------------------------------------------------- per row

export interface GrantLevelChange {
  /** The button's label. */
  label: string;
  /** The same grant at the other level; `PUT /grants` changes the level of a grant that exists. */
  request: GrantRequest;
}

/**
 * The other level for a user or group grant; the owner, `everyone` and a
 * service principal (CAN_USE only, ADR 0020) have none.
 */
export function grantLevelChange(row: GrantRow): GrantLevelChange | null {
  if (
    row.isOwner ||
    row.type === "everyone" ||
    row.type === "service_principal"
  ) {
    return null;
  }
  const isAdmin = row.permission === "CAN_MANAGE";
  const permission = isAdmin ? "CAN_USE" : "CAN_MANAGE";
  return {
    // A group is not a user or an admin: its label names the level, in the
    // words the level column shows ("Can use", "Can manage").
    label:
      row.type === "group"
        ? `Make ${permissionLabel(permission)}`
        : isAdmin
          ? "Make user"
          : "Make admin",
    request: { type: row.type, name: row.name, permission },
  };
}

/**
 * The question to ask before the change when it lowers the actor's own grant
 * from `CAN_MANAGE`: an app admin who does so loses this page. `null` for any
 * other change. A platform admin keeps `CAN_MANAGE` without a grant, so for
 * them it is not a lock-out. A group the actor belongs to cannot be seen from
 * here, so only a grant that names the actor's own email is checked.
 */
export function grantLevelChangePrompt(
  row: GrantRow,
  actor: Actor | null,
): string | null {
  const change = grantLevelChange(row);
  if (change === null || actor === null || actor.isPlatformAdmin) return null;
  const isOwnGrant =
    row.type === "user" && row.name.toLowerCase() === actor.email.toLowerCase();
  const isLowering =
    row.permission === "CAN_MANAGE" && change.request.permission === "CAN_USE";
  if (!isOwnGrant || !isLowering) return null;
  return `Lower your own access to ${permissionLabel(change.request.permission)}? You could no longer manage this app, and you may lose this page.`;
}

export function grantRemoveAvailability(
  row: GrantRow,
  actor: Actor | null,
): Availability {
  if (actor === null) return { isAllowed: false, reason: REASON_UNKNOWN_ACTOR };
  if (!actor.canManage) return { isAllowed: false, reason: REASON_NOT_MANAGER };
  if (row.isOwner) {
    return {
      isAllowed: false,
      reason:
        "The owner always manages the app; only a platform admin changes it.",
    };
  }
  return { isAllowed: true };
}

export function grantRemovePrompt(row: GrantRow, actorEmail: string): string {
  const who =
    row.type === "everyone"
      ? "everyone"
      : `${row.type === "group" ? "group " : ""}${row.name}`;
  const question = `Remove ${who} (${permissionLabel(row.permission)})?`;
  const isOwnAccess =
    row.type === "user" && row.name.toLowerCase() === actorEmail.toLowerCase();
  return isOwnAccess
    ? `${question} This is your own access; you may lose this page.`
    : question;
}
