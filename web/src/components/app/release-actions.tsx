"use client";

import { EllipsisIcon, LoaderCircleIcon } from "lucide-react";
import { Fragment, useRef } from "react";
import type { Ref } from "react";

import type { AppDetail, ReleaseSummary } from "@/lib/api";
import type { Actor, ReleaseAction } from "@/lib/app/actions";
import { releaseMenuItems } from "@/lib/app/releases";
import type { ReleaseMenuAction } from "@/lib/app/releases";
import type { WriteState } from "@/lib/app/writes";
import { Button as UiButton } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { AppActions } from "./use-app-actions";

/** The actions that call the server at once; Edit and Source only open a panel. */
export type ReleaseWrite = Exclude<ReleaseAction, "edit">;

/** The menu action whose call can be running, and the words for it. */
const BUSY_LABELS: Partial<Record<ReleaseMenuAction, string>> = {
  activate: "Activating",
  lock: "Locking",
  unlock: "Unlocking",
  delete: "Deleting",
  rename: "Saving",
  tag: "Saving",
};

/**
 * The actions that open a panel under the table, which takes the focus; after
 * the others the menu gives it back to its trigger.
 */
const OPENS_A_PANEL: ReadonlySet<ReleaseMenuAction> = new Set([
  "rename",
  "tag",
  "view-source",
  "delete",
]);

/**
 * Where a menu action stands in the page's one write queue. Rename and Tag are
 * the one edit call; View source is a read, which a running write does not hold.
 */
function writeStateOfAction(
  actions: AppActions,
  action: ReleaseMenuAction,
  releaseNo: number,
): WriteState {
  if (action === "view-source") return "idle";
  const call = action === "rename" || action === "tag" ? "edit" : action;
  return actions.stateOf(`${call}:${String(releaseNo)}`);
}

export interface ReleaseRowMenuProps {
  app: AppDetail;
  release: ReleaseSummary;
  actor: Actor | null;
  actions: AppActions;
  isViewingSource: boolean;
  /** The trigger, so the section can put focus back on it after a panel closes. */
  triggerRef: Ref<HTMLButtonElement>;
  onSelect: (action: ReleaseMenuAction, release: ReleaseSummary) => void;
}

/**
 * The "…" menu of one release row. An action that cannot be used stays in the
 * menu, reachable with the arrow keys, with its reason written under it; choosing
 * it does nothing. While a call of this row runs, the trigger turns into a
 * spinner with the words beside it.
 */
export function ReleaseRowMenu({
  app,
  release,
  actor,
  actions,
  isViewingSource,
  triggerRef,
  onSelect,
}: ReleaseRowMenuProps) {
  const releaseNo = release.release_no;
  const chosenRef = useRef<ReleaseMenuAction | null>(null);
  const items = releaseMenuItems(release, app, actor);
  const busyItem = items.find(
    (item) => writeStateOfAction(actions, item.action, releaseNo) === "busy",
  );
  const busyLabel =
    busyItem === undefined ? null : (BUSY_LABELS[busyItem.action] ?? null);

  return (
    <div className="flex items-center gap-2">
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <UiButton
            ref={triggerRef}
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label={`Actions for release ${String(releaseNo)}`}
          >
            <EllipsisIcon aria-hidden="true" />
          </UiButton>
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align="end"
          className="w-64"
          onCloseAutoFocus={(event) => {
            const chosen = chosenRef.current;
            chosenRef.current = null;
            // The panel the action opened focuses itself; the trigger must not take it back.
            if (chosen !== null && OPENS_A_PANEL.has(chosen)) {
              event.preventDefault();
            }
          }}
        >
          <DropdownMenuLabel>Release {releaseNo}</DropdownMenuLabel>
          <DropdownMenuSeparator />
          {items.map((item) => {
            const state = writeStateOfAction(actions, item.action, releaseNo);
            const isUsable = item.availability.isAllowed && state === "idle";
            const label =
              item.action === "view-source" && isViewingSource
                ? "Hide source"
                : item.label;
            const note = !item.availability.isAllowed
              ? item.availability.reason
              : state === "blocked"
                ? "Another change is being saved; wait for it."
                : state === "busy"
                  ? `${BUSY_LABELS[item.action] ?? "Working"}…`
                  : item.hint;
            return (
              <Fragment key={item.action}>
                {item.isDestructive ? <DropdownMenuSeparator /> : null}
                <DropdownMenuItem
                  variant={item.isDestructive ? "destructive" : "default"}
                  // Not Radix's `disabled`: that would skip the item, and its reason, on the keyboard.
                  aria-disabled={isUsable ? undefined : true}
                  data-unavailable={isUsable ? undefined : ""}
                  className="flex-col items-start gap-0.5 data-unavailable:text-muted-foreground"
                  onSelect={(event) => {
                    if (!isUsable) {
                      event.preventDefault();
                      return;
                    }
                    chosenRef.current = item.action;
                    onSelect(item.action, release);
                  }}
                >
                  <span>{label}</span>
                  {note === null ? null : (
                    <span className="text-xs whitespace-normal text-muted-foreground">
                      {note}
                    </span>
                  )}
                </DropdownMenuItem>
              </Fragment>
            );
          })}
        </DropdownMenuContent>
      </DropdownMenu>
      {busyLabel === null ? null : (
        <span className="inline-flex items-center gap-1 text-sm text-muted-foreground">
          <LoaderCircleIcon
            className="size-4 animate-spinner"
            aria-hidden="true"
          />
          {busyLabel}
        </span>
      )}
    </div>
  );
}
