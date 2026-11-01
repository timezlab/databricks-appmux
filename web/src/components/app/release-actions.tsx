"use client";

import type { AppDetail, ReleaseSummary } from "@/lib/api";
import {
  canViewSource,
  hasManualLock,
  releaseActionAvailability,
} from "@/lib/app/actions";
import type { Actor, ReleaseAction } from "@/lib/app/actions";
import { unavailableReasons } from "@/lib/app/reasons";
import { Button } from "@/components/button";
import { ConfirmButton } from "@/components/confirm-button";
import type { AppActions } from "./use-app-actions";

/** The actions that call the server at once; Edit and Source only open a panel. */
export type ReleaseWrite = Exclude<ReleaseAction, "edit">;

export interface ReleaseActionsProps {
  app: AppDetail;
  release: ReleaseSummary;
  actor: Actor | null;
  actions: AppActions;
  isEditing: boolean;
  isViewingSource: boolean;
  onWrite: (action: ReleaseWrite, release: ReleaseSummary) => void;
  onToggleEdit: (release: ReleaseSummary) => void;
  onToggleSource: (release: ReleaseSummary) => void;
}

/** The buttons of one release row, with the reason for every one that is off written under them. */
export function ReleaseActions({
  app,
  release,
  actor,
  actions,
  isEditing,
  isViewingSource,
  onWrite,
  onToggleEdit,
  onToggleSource,
}: ReleaseActionsProps) {
  const releaseNo = release.release_no;
  const availability = (action: ReleaseAction) =>
    releaseActionAvailability(action, release, app, actor);
  const lockAction = hasManualLock(release) ? "unlock" : "lock";
  const activate = availability("activate");
  const lock = availability(lockAction);
  const remove = availability("delete");
  const edit = availability("edit");
  const source = canViewSource(actor);

  const keyOf = (action: ReleaseWrite) => `${action}:${releaseNo}`;
  const stateOf = (action: ReleaseWrite) => actions.stateOf(keyOf(action));
  const spokenName = (
    <>
      {" "}
      <span className="sr-only">release {releaseNo}</span>
    </>
  );

  const reasons = unavailableReasons([
    { label: "Activate", availability: activate },
    { label: lockAction === "lock" ? "Lock" : "Unlock", availability: lock },
    { label: "Delete", availability: remove },
    { label: "Edit", availability: edit },
    { label: "View source", availability: source },
  ]);

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="small"
          isBusy={stateOf("activate") === "busy"}
          busyLabel="Activating"
          disabled={!activate.isAllowed || stateOf("activate") === "blocked"}
          onClick={() => {
            onWrite("activate", release);
          }}
        >
          Activate{spokenName}
        </Button>
        <Button
          size="small"
          isBusy={stateOf(lockAction) === "busy"}
          busyLabel={lockAction === "lock" ? "Locking" : "Unlocking"}
          disabled={!lock.isAllowed || stateOf(lockAction) === "blocked"}
          onClick={() => {
            onWrite(lockAction, release);
          }}
        >
          {lockAction === "lock" ? "Lock" : "Unlock"}
          {spokenName}
        </Button>
        <Button
          size="small"
          aria-expanded={isEditing}
          disabled={!edit.isAllowed}
          onClick={() => {
            onToggleEdit(release);
          }}
        >
          Edit{spokenName}
        </Button>
        <Button
          size="small"
          aria-expanded={isViewingSource}
          disabled={!source.isAllowed}
          onClick={() => {
            onToggleSource(release);
          }}
        >
          View source{spokenName}
        </Button>
        <ConfirmButton
          size="small"
          label="Delete"
          prompt={`Delete release ${releaseNo}? Its snapshot is removed and it cannot be activated again.`}
          confirmLabel={`Yes, delete release ${releaseNo}`}
          isBusy={stateOf("delete") === "busy"}
          busyLabel="Deleting"
          isDisabled={!remove.isAllowed || stateOf("delete") === "blocked"}
          onConfirm={() => {
            onWrite("delete", release);
          }}
        />
      </div>
      {reasons.length === 0 ? null : (
        <ul className="text-sm text-muted-foreground">
          {reasons.map((reason) => (
            <li key={reason}>{reason}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
