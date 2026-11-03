"use client";

import { useState } from "react";

import { createRelease, restartApp, startApp, stopApp } from "@/lib/api";
import type { AppDetail } from "@/lib/api";
import type { Availability } from "@/lib/app/actions";
import { appControlMessage, updateOutcomeMessage } from "@/lib/app/outcomes";
import type { AppControlAction } from "@/lib/app/outcomes";
import { Button } from "@/components/button";
import type { AppActions } from "./use-app-actions";

function controlCall(action: AppControlAction, slug: string) {
  switch (action) {
    case "start":
      return startApp(slug);
    case "stop":
      return stopApp(slug);
    case "restart":
      return restartApp(slug);
  }
}

export interface AppControls {
  /** The last refused call, shown under the buttons until the next press. */
  failure: { error: unknown } | null;
  /** Fetches the source folder and builds a release from it (`POST /releases`). */
  runUpdate: () => void;
  runControl: (action: AppControlAction) => void;
}

/**
 * Update, Start, Stop and Restart as calls through the page's one write queue.
 * Overview and Deployments both offer Update; each keeps its own refusal so it
 * shows where the button was pressed.
 */
export function useAppControls(
  app: AppDetail,
  actions: AppActions,
): AppControls {
  const [failure, setFailure] = useState<{ error: unknown } | null>(null);

  async function update(): Promise<void> {
    setFailure(null);
    const result = await actions.run(
      "update",
      () => createRelease(app.slug),
      updateOutcomeMessage,
    );
    if (!result.ok) setFailure({ error: result.error });
  }

  async function control(action: AppControlAction): Promise<void> {
    setFailure(null);
    const result = await actions.run(
      action,
      () => controlCall(action, app.slug),
      appControlMessage(action),
    );
    if (!result.ok) setFailure({ error: result.error });
  }

  return {
    failure,
    runUpdate: () => {
      void update();
    },
    runControl: (action) => {
      void control(action);
    },
  };
}

export interface UpdateButtonProps {
  availability: Availability;
  actions: AppActions;
  onUpdate: () => void;
}

/** The primary button of an app: a new release from the source folder. */
export function UpdateButton({
  availability,
  actions,
  onUpdate,
}: UpdateButtonProps) {
  const state = actions.stateOf("update");
  return (
    <Button
      variant="primary"
      isBusy={state === "busy"}
      busyLabel="Updating"
      disabled={!availability.isAllowed || state === "blocked"}
      onClick={onUpdate}
    >
      Update
    </Button>
  );
}
