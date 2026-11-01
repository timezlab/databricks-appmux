"use client";

import { useReducer, useRef } from "react";

import { IDLE_ACTION, actionReducer } from "@/lib/platform/action";
import type { ActionOutcome, ActionState } from "@/lib/platform/action";
import { usePlatformContext } from "./platform-context";

export interface WriteAction {
  state: ActionState;
  isBusy: boolean;
  /**
   * Runs one write. `perform` makes the call and returns the sentence to show;
   * a throw is kept as the error to show. Resolves `true` when it succeeded.
   * A second call while one is in flight does nothing.
   */
  run: (perform: () => Promise<ActionOutcome>) => Promise<boolean>;
  /** Repeats the last write (the "Try again" of an error). */
  retry: () => void;
  /** Hides the shown result. */
  clear: () => void;
}

/**
 * The state of one write button or form of the platform page. After a success
 * it announces the outcome and asks the page to fetch its data again, so the
 * lists show what the server now holds.
 */
export function useWriteAction(): WriteAction {
  const { refresh, announce } = usePlatformContext();
  const [state, dispatch] = useReducer(actionReducer, IDLE_ACTION);
  // The reducer drops a second "started", but only this guard stops the call itself.
  const isInFlightRef = useRef(false);
  const lastPerformRef = useRef<(() => Promise<ActionOutcome>) | null>(null);

  async function run(perform: () => Promise<ActionOutcome>): Promise<boolean> {
    if (isInFlightRef.current) return false;
    isInFlightRef.current = true;
    lastPerformRef.current = perform;
    dispatch({ type: "started" });
    try {
      const outcome = await perform();
      dispatch({ type: "succeeded", outcome });
      announce(outcome.title);
      refresh();
      return true;
    } catch (error) {
      // Shown by <ActionResult> as an error notice; nothing else to do with it.
      dispatch({ type: "failed", error });
      return false;
    } finally {
      isInFlightRef.current = false;
    }
  }

  return {
    state,
    isBusy: state.status === "busy",
    run,
    retry: () => {
      const perform = lastPerformRef.current;
      if (perform !== null) void run(perform);
    },
    clear: () => {
      dispatch({ type: "cleared" });
    },
  };
}
