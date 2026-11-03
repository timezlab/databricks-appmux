/**
 * The apps section of the launcher (design 10.1, 9.1): which button each app
 * gets, and how the list is filtered. The page only draws what comes back.
 *
 * The API decides who may open what (`GET /apps` lists only apps the actor can
 * open, and fills `open_modes` only for the platform admin who must choose);
 * this module never recomputes a permission, it follows those fields.
 */
import type { AppState, AppSummary, OpenMode } from "@/lib/api";
import { appPagePath } from "@/lib/app/params";
import { permissionLabel } from "@/lib/app/sharing";
import { appLink } from "@/lib/slug-link";

/**
 * A search box appears once there are more than ten apps: ten rows still fit
 * on one screen and are read at a glance. The contract bounds a list at about
 * 100 apps.
 */
export const APP_FILTER_MIN_COUNT = 11;

/**
 * The order of the states, what needs attention first: a crashed app, then
 * one that is starting (a start can hang, as a Node.js app does in a small
 * memory budget), then a stopped one, then the running ones.
 */
const ATTENTION_RANK: Readonly<Record<AppState, number>> = {
  crashed: 0,
  starting: 1,
  stopped: 2,
  running: 3,
};

/** "without token" is the default of design 9.1, so it comes first. */
const OPEN_MODE_ORDER: readonly OpenMode[] = ["without_token", "as_user"];

// The route of a child opens only once its health check passes (constitution
// II), so a link to a stopped, starting or crashed app would land on a 404 or
// 503. The reason says what has to happen instead, and who does it: a person
// who manages the app starts it from its page; anyone else asks its admin.
const NOT_OPEN_REASON: Record<
  Exclude<AppState, "running">,
  { manager: string; visitor: string }
> = {
  stopped: {
    manager: "Not running. Start it from Manage.",
    visitor: "Not running. Ask the app admin to start it.",
  },
  starting: {
    manager: "Starting. Open appears once its health check passes.",
    visitor: "Starting. Open appears once its health check passes.",
  },
  crashed: {
    manager: "Crashed. Restart it from Manage.",
    visitor: "Crashed. Ask the app admin to restart it.",
  },
};
const INVALID_SLUG_REASON =
  "This app's address is not valid, so it cannot be opened from here.";

// The link itself is not part of the action: `AppLink` and `openAppWithMode`
// build `/<slug>/` from the slug through `appLink`, which checks the slug
// first. The action only says which control to draw, and `unavailable` is
// returned for a slug that fails that check.
export type AppOpenAction =
  /** One Open link: the app is not OBO-delegated, or the actor has a grant. */
  | { kind: "open" }
  /** The platform admin chooses; `modes` are in the order they are shown. */
  | { kind: "choose-mode"; modes: readonly OpenMode[] }
  | { kind: "unavailable"; reason: string };

export interface LauncherAppRow {
  slug: string;
  displayName: string;
  state: AppState;
  /** The person's level on the app, in words ("Can manage"). */
  levelLabel: string;
  open: AppOpenAction;
  /** The app page (T173), for `CAN_MANAGE` only; `null` otherwise or for an invalid slug. */
  manageHref: string | null;
}

function openAction(app: AppSummary): AppOpenAction {
  if (appLink(app.slug) === null) {
    return { kind: "unavailable", reason: INVALID_SLUG_REASON };
  }
  if (app.state !== "running") {
    const reasons = NOT_OPEN_REASON[app.state];
    return {
      kind: "unavailable",
      reason:
        app.permission === "CAN_MANAGE" ? reasons.manager : reasons.visitor,
    };
  }
  const modes = OPEN_MODE_ORDER.filter((mode) => app.open_modes.includes(mode));
  return modes.length === 0 ? { kind: "open" } : { kind: "choose-mode", modes };
}

// Names compare without regard to case or accents, and digits as numbers, so
// "App 2" comes before "App 10" and "alpha" is not placed after every capital.
const NAME_COLLATOR = new Intl.Collator("en", {
  sensitivity: "base",
  numeric: true,
});

function compareByAttentionThenName(a: AppSummary, b: AppSummary): number {
  return (
    ATTENTION_RANK[a.state] - ATTENTION_RANK[b.state] ||
    NAME_COLLATOR.compare(a.display_name, b.display_name) ||
    NAME_COLLATOR.compare(a.slug, b.slug)
  );
}

/**
 * One row per app: what needs attention first (`ATTENTION_RANK`), then by
 * display name (then slug), since a person looks an app up by its name.
 */
export function launcherAppRows(
  items: readonly AppSummary[],
): LauncherAppRow[] {
  return items.toSorted(compareByAttentionThenName).map((app) => ({
    slug: app.slug,
    displayName: app.display_name,
    state: app.state,
    levelLabel: permissionLabel(app.permission),
    open: openAction(app),
    // `appPagePath` is the one builder of the app page's address (T173); it
    // returns null for a slug that fails the contract pattern.
    manageHref: app.permission === "CAN_MANAGE" ? appPagePath(app.slug) : null,
  }));
}

export function shouldOfferAppFilter(appCount: number): boolean {
  return appCount >= APP_FILTER_MIN_COUNT;
}

/** Rows whose name or slug contains `query`, ignoring case and surrounding spaces. */
export function filterAppRows(
  rows: readonly LauncherAppRow[],
  query: string,
): readonly LauncherAppRow[] {
  const needle = query.trim().toLowerCase();
  if (needle === "") return rows;
  return rows.filter(
    (row) =>
      row.displayName.toLowerCase().includes(needle) ||
      row.slug.toLowerCase().includes(needle),
  );
}

/**
 * The rows to draw. The query counts only while the filter box is on screen: a
 * poll can shrink the list under `APP_FILTER_MIN_COUNT` while a query is typed,
 * and the box (the only way to clear the query) goes with it.
 */
export function visibleAppRows(
  rows: readonly LauncherAppRow[],
  query: string,
): readonly LauncherAppRow[] {
  return shouldOfferAppFilter(rows.length) ? filterAppRows(rows, query) : rows;
}

/**
 * The disclosure that explains "without token" and "as a user"; only when some
 * app offers both. An app that offers one mode has nothing to tell apart.
 */
export function shouldExplainOpenModes(
  rows: readonly LauncherAppRow[],
): boolean {
  return rows.some(
    (row) => row.open.kind === "choose-mode" && row.open.modes.length > 1,
  );
}
