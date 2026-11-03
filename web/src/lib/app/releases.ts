/**
 * The row menu of a release in Deployments: which actions it lists, in which
 * order, and whether each can be used now (with the reason when not). The
 * rules themselves are `releaseActionAvailability` and `canViewSource`
 * (actions.ts); this file only turns them into the menu a row shows.
 */
import type { AppDetail, ReleaseSummary } from "@/lib/api";
import {
  canViewSource,
  hasManualLock,
  releaseActionAvailability,
} from "./actions";
import type { Actor, Availability } from "./actions";
import { activeRelease } from "./status";

export type ReleaseMenuAction =
  "activate" | "rename" | "tag" | "lock" | "unlock" | "view-source" | "delete";

export interface ReleaseMenuItem {
  action: ReleaseMenuAction;
  label: string;
  availability: Availability;
  /** One line under the label about what the action does here ("Rolls back to this release."). */
  hint: string | null;
  /** Asks first, and is drawn as a destructive item. */
  isDestructive: boolean;
}

function activateHint(
  release: ReleaseSummary,
  active: ReleaseSummary | null,
): string | null {
  if (release.status === "failed") return "Tries this release again.";
  if (
    active !== null &&
    release.release_no < active.release_no &&
    release.status === "superseded"
  ) {
    return "Rolls back to this release.";
  }
  return null;
}

/** The menu of one release row: Activate, Rename, Tag, Lock or Unlock, View source, then Delete. */
export function releaseMenuItems(
  release: ReleaseSummary,
  app: AppDetail,
  actor: Actor | null,
): ReleaseMenuItem[] {
  const availability = (
    action: Parameters<typeof releaseActionAvailability>[0],
  ) => releaseActionAvailability(action, release, app, actor);
  const lockAction = hasManualLock(release) ? "unlock" : "lock";
  const edit = availability("edit");
  return [
    {
      action: "activate",
      label: "Activate",
      availability: availability("activate"),
      hint: activateHint(release, activeRelease(app)),
      isDestructive: false,
    },
    {
      action: "rename",
      label: "Rename",
      availability: edit,
      hint: null,
      isDestructive: false,
    },
    {
      action: "tag",
      label: "Tag",
      availability: edit,
      hint: null,
      isDestructive: false,
    },
    {
      action: lockAction,
      label: lockAction === "lock" ? "Lock" : "Unlock",
      availability: availability(lockAction),
      hint: null,
      isDestructive: false,
    },
    {
      action: "view-source",
      label: "View source",
      availability: canViewSource(actor),
      hint: null,
      isDestructive: false,
    },
    {
      action: "delete",
      label: "Delete",
      availability: availability("delete"),
      hint: null,
      isDestructive: true,
    },
  ];
}
