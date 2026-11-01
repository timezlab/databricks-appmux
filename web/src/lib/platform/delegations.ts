/**
 * Delegations on the platform page (design 8.1, admin-api `PUT` and `DELETE
 * /apps/{app}/delegation/{kind}`): which credential an app may be handed, and
 * what a platform admin can do about it right now.
 *
 * Granting is a review gate, so it carries a reason. Revoking carries one too,
 * recorded in the audit log. A shared-SP grant is possible only while the
 * `shared_sp_delegation_allowed` setting is on; a revoke never is blocked.
 */
import { DelegationReasonRequest } from "@/lib/api/schemas";
import type { DelegationKind, PlatformApp } from "@/lib/api/schemas";
import { formErrors } from "./forms";
import type { FormParse } from "./forms";

export type DelegationAction = "grant" | "revoke";

export interface DelegationInfo {
  label: string;
  /** What the child receives while the delegation holds. */
  receives: string;
  grantEffect: string;
  revokeEffect: string;
}

export const DELEGATION_INFO: Record<DelegationKind, DelegationInfo> = {
  obo: {
    label: "On behalf of the visitor (OBO)",
    receives: "The visitor's own token, with the appmux app's scope set.",
    grantEffect:
      "The app receives each visitor's token from the next request, and can act as that visitor within the scopes.",
    revokeEffect: "OBO stops at the next request.",
  },
  shared_sp: {
    label: "Shared service principal",
    receives:
      "A short-lived token of the appmux service principal, in a file in the app's run directory.",
    grantEffect:
      "The app can use everything the appmux service principal can reach, including every secret scope it can read.",
    revokeEffect:
      "A token already lent expires within its hour; the app's resources stay and are flagged on its page.",
  },
};

const SWITCH_OFF_REASON =
  "Turn on the shared_sp_delegation_allowed setting before granting this.";

export interface DelegationCell {
  kind: DelegationKind;
  isGranted: boolean;
  stateText: "Allowed" | "Not allowed";
  /** What the row's button does, or `null` when nothing can be done now. */
  action: DelegationAction | null;
  /** Why `action` is `null`. */
  blockedReason: string | null;
}

function isGranted(app: PlatformApp, kind: DelegationKind): boolean {
  return kind === "obo" ? app.obo_delegated : app.shared_sp_delegated;
}

export function delegationCell(
  app: PlatformApp,
  kind: DelegationKind,
  isSharedSpAllowed: boolean,
): DelegationCell {
  const granted = isGranted(app, kind);
  if (granted) {
    return {
      kind,
      isGranted: true,
      stateText: "Allowed",
      action: "revoke",
      blockedReason: null,
    };
  }
  const isBlocked = kind === "shared_sp" && !isSharedSpAllowed;
  return {
    kind,
    isGranted: false,
    stateText: "Not allowed",
    action: isBlocked ? null : "grant",
    blockedReason: isBlocked ? SWITCH_OFF_REASON : null,
  };
}

/**
 * The reason a reason panel opened for `action` can no longer be submitted
 * (another admin changed the app, or the master switch moved while the person
 * was typing), or `null`. The page checks this on every render and at submit,
 * so a stale intention is never sent.
 */
export function delegationActionProblem(
  app: PlatformApp,
  kind: DelegationKind,
  action: DelegationAction,
  isSharedSpAllowed: boolean,
): string | null {
  const current = delegationCell(app, kind, isSharedSpAllowed);
  if (current.action === action) return null;
  if (current.blockedReason !== null) return current.blockedReason;
  return action === "grant"
    ? `${DELEGATION_INFO[kind].label} is already allowed for ${app.slug}.`
    : `${DELEGATION_INFO[kind].label} is not allowed for ${app.slug}, so there is nothing to revoke.`;
}

/** The body of the grant, and the query of the revoke: the reason and the latest `apps` version. */
export function parseDelegationReason(
  app: PlatformApp,
  text: string,
): FormParse<DelegationReasonRequest> {
  const parsed = DelegationReasonRequest.safeParse({
    version: app.version,
    reason: text.trim(),
  });
  return parsed.success
    ? { ok: true, value: parsed.data }
    : { ok: false, errors: formErrors(parsed.error.issues) };
}
