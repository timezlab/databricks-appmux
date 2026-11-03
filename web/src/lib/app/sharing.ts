/**
 * Who may open and manage the app: its grants (design 9.1). The owner is not a
 * grant row; the page shows them as a fixed first row. A platform admin holds
 * `CAN_MANAGE` implicitly and is not listed, because there is no row to show.
 */
import { GrantRequest } from "@/lib/api";
import type { AppDetail, AppPermission, GrantType } from "@/lib/api";
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

// -------------------------------------------------------------------- form

/** What the inputs hold. `everyone` is `*` with `CAN_USE`, which the form fixes. */
export interface GrantFormValues {
  type: GrantType;
  name: string;
  permission: AppPermission;
}

export const EMPTY_GRANT_FORM: GrantFormValues = {
  type: "user",
  name: "",
  permission: "CAN_USE",
};

export function changeGrantType(
  values: GrantFormValues,
  type: GrantType,
): GrantFormValues {
  if (type === "everyone") {
    return { type, name: EVERYONE_NAME, permission: "CAN_USE" };
  }
  return {
    ...values,
    type,
    name: values.name === EVERYONE_NAME ? "" : values.name,
  };
}

export function buildGrantRequest(
  app: AppDetail,
  values: GrantFormValues,
): FormResult<GrantRequest> {
  const errors: FormErrors = {};
  let name = values.name.trim();

  if (values.type === "everyone") {
    name = EVERYONE_NAME;
    if (values.permission !== "CAN_USE") {
      errors.permission = "Everyone can only be given CAN_USE.";
    }
  } else if (values.type === "user") {
    // The contract wants a lower-case email; people type capitals.
    name = name.toLowerCase();
    if (name === "") {
      errors.name = "Enter an email address.";
    } else if (!name.includes("@")) {
      errors.name = "Enter an email address, such as user@example.com.";
    } else if (
      name === app.owner.toLowerCase() &&
      values.permission !== "CAN_MANAGE"
    ) {
      errors.name =
        "This is the app's owner, who always manages it; a lower level is refused.";
    }
  } else if (name === "") {
    errors.name = "Enter the name of a Databricks group.";
  }
  if (Object.keys(errors).length > 0) return formFailure(errors);

  const parsed = GrantRequest.safeParse({
    type: values.type,
    name,
    permission: values.permission,
  });
  if (!parsed.success) return formFailure(errorsFromZod(parsed.error));
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
  return {
    label: isAdmin ? "Make user" : "Make admin",
    request: {
      type: row.type,
      name: row.name,
      permission: isAdmin ? "CAN_USE" : "CAN_MANAGE",
    },
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
