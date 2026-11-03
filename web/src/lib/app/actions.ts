/**
 * Who may do what on the app page, decided from API fields only. The server
 * checks every call again (and stays right when this page is stale), so these
 * rules only decide which buttons are enabled and what the disabled ones say.
 * When a rule is not certain, the button is disabled with its reason rather
 * than offered: the person can always reload and look again.
 */
import { ApiError } from "@/lib/api";
import { assertNever } from "@/lib/assert-never";
import type {
  AppDetail,
  AppPermission,
  AppSummary,
  MeResponse,
  ReleaseStatus,
  ReleaseSummary,
} from "@/lib/api";

export const REASON_NOT_MANAGER = "Only an admin of this app can do this.";
export const REASON_UNKNOWN_ACTOR = "Checking what you may do here...";
const REASON_RELEASE_IN_PROGRESS =
  "This release is still on its way (being built or started); wait for it to finish.";

/** Why a button is off, or that it is on. The reason is shown as the button's hint. */
export type Availability =
  { isAllowed: true } | { isAllowed: false; reason: string };

const ALLOWED: Availability = { isAllowed: true };

function refused(reason: string): Availability {
  return { isAllowed: false, reason };
}

// ------------------------------------------------------------------- actor

export interface Actor {
  email: string;
  isPlatformAdmin: boolean;
  /** The level the launcher list reports for this app, or `null` when the app is not in the list. */
  permission: AppPermission | null;
  /** `CAN_MANAGE` on the app, which a platform admin holds on every app without a grant row. */
  canManage: boolean;
}

/**
 * The actor from the two answers the page already has: `/me` (who, and whether
 * a platform admin) and the launcher list (the level on this app). `null`
 * while either is missing, so no button is on before it is known.
 */
export function resolveActor(
  me: MeResponse | null,
  summaries: readonly AppSummary[] | null,
  slug: string,
): Actor | null {
  if (me === null || summaries === null) return null;
  const permission =
    summaries.find((summary) => summary.slug === slug)?.permission ?? null;
  return {
    email: me.email,
    isPlatformAdmin: me.is_platform_admin,
    permission,
    canManage: permission === "CAN_MANAGE" || me.is_platform_admin,
  };
}

/** The gate every change on the page shares: the actor is known and holds `CAN_MANAGE` on the app. */
export function manageAvailability(actor: Actor | null): Availability {
  if (actor === null) return refused(REASON_UNKNOWN_ACTOR);
  if (!actor.canManage) return refused(REASON_NOT_MANAGER);
  return ALLOWED;
}

// ---------------------------------------------------------------- releases

const IN_PROGRESS_STATUSES: readonly ReleaseStatus[] = [
  "pending",
  "building",
  "starting",
  "healthy",
];

/** From the request to build until the release is active (or fails). */
export function isReleaseInProgress(status: ReleaseStatus): boolean {
  return IN_PROGRESS_STATUSES.includes(status);
}

/**
 * Only a lock set by hand is stored. The active release is protected while it
 * is active, with no mark, so it never counts here.
 */
export function hasManualLock(release: ReleaseSummary): boolean {
  return release.locked_by !== null;
}

export function manualLockCount(app: AppDetail): number {
  return app.releases.filter(hasManualLock).length;
}

export function hasActiveRelease(app: AppDetail): boolean {
  return app.releases.some((release) => release.status === "active");
}

export type ReleaseAction = "activate" | "lock" | "unlock" | "delete" | "edit";

export function releaseActionAvailability(
  action: ReleaseAction,
  release: ReleaseSummary,
  // Kept in the signature for the rules that depend on the app as a whole
  // (a cap on locks), which the API does not give yet.
  _app: AppDetail,
  actor: Actor | null,
): Availability {
  const gate = manageAvailability(actor);
  if (!gate.isAllowed) return gate;

  switch (action) {
    case "activate":
      if (isReleaseInProgress(release.status)) {
        return refused(REASON_RELEASE_IN_PROGRESS);
      }
      // A failed target is retried with Activate; the active release is only
      // re-activated once a failed target has taken its place as target.
      if (release.status === "active" && release.is_target) {
        return refused("This release is already active.");
      }
      return ALLOWED;
    case "lock":
      // Design 5.2: any live release may be locked by hand. The only refusal
      // the contract names is the cap (`max_locked_releases`), which the page
      // cannot see, so the server's 409 is shown with a reload prompt.
      if (hasManualLock(release)) {
        return refused(`Already locked by ${release.locked_by ?? "someone"}.`);
      }
      return ALLOWED;
    case "unlock":
      if (hasManualLock(release)) return ALLOWED;
      if (release.status === "active") {
        return refused(
          "The active release is protected while it is active; it has no lock to remove.",
        );
      }
      return refused("This release is not locked.");
    case "edit":
      // Name and tags are labels; they change nothing about how the release runs.
      return ALLOWED;
    case "delete":
      if (release.status === "active") {
        return refused("The active release cannot be deleted.");
      }
      if (release.is_target) {
        return refused(
          "This release is the target; activate another release first.",
        );
      }
      if (isReleaseInProgress(release.status)) {
        return refused(
          "A release that is being built or started cannot be deleted.",
        );
      }
      if (hasManualLock(release)) {
        return refused("Locked; unlock this release first.");
      }
      return ALLOWED;
    default:
      return assertNever(action);
  }
}

// -------------------------------------------------------------------- app

export type AppAction = "update" | "restart" | "stop" | "start";

export function appActionAvailability(
  action: AppAction,
  app: AppDetail,
  actor: Actor | null,
): Availability {
  const gate = manageAvailability(actor);
  if (!gate.isAllowed) return gate;

  const isMeantToRun = app.config.desired_state === "running";
  switch (action) {
    case "update":
      return ALLOWED;
    case "stop":
      return isMeantToRun ? ALLOWED : refused("The app is already stopped.");
    case "start":
      if (isMeantToRun) {
        return refused(
          "The app is already meant to run; use Restart to start it again.",
        );
      }
      return hasActiveRelease(app) ? ALLOWED : refused(NEEDS_RELEASE_REASON);
    case "restart":
      if (!isMeantToRun) {
        return refused("The app is stopped; use Start.");
      }
      return hasActiveRelease(app) ? ALLOWED : refused(NEEDS_RELEASE_REASON);
    default:
      return assertNever(action);
  }
}

const NEEDS_RELEASE_REASON =
  "There is no active release to run yet; use Update to fetch one.";

/**
 * The release source viewer. The contract grants `release.view_source` to
 * `CAN_MANAGE` on the app, which a platform admin holds on every app.
 */
export function canViewSource(actor: Actor | null): Availability {
  return manageAvailability(actor);
}

// ------------------------------------------------------------------ errors

/**
 * The 409 codes that say the page holds something the server no longer
 * does (contracts/admin-api.md): `changed_by_others` for a stale `version`,
 * `app_missing` for an app that is gone. Every other 409 (`slug_taken`,
 * `no_release`, `release_protected`, …) refuses the write as it stands, and
 * reading the app again would only bury that refusal under a reload notice.
 */
const RELOAD_CONFLICT_CODES: ReadonlySet<string> = new Set([
  "changed_by_others",
  "app_missing",
]);

/**
 * After a stale version or a vanished app (409) or a 404 (the release or
 * app is gone) the page reads the app again, so the person continues from
 * what is true now.
 */
export function shouldReloadAfterError(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false;
  return (
    error.status === 404 ||
    (error.status === 409 && RELOAD_CONFLICT_CODES.has(error.code))
  );
}
