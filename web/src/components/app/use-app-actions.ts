import { useEffect, useState } from "react";

import { createWriteController, writeStateOf } from "@/lib/app/writes";
import type {
  WriteMessage,
  WriteNotice,
  WriteResult,
  WriteState,
} from "@/lib/app/writes";

export interface AppActions {
  /** `busy` for the write in flight, `blocked` for every other one meanwhile. */
  stateOf: (key: string) => WriteState;
  /** One write at a time, and a read of the app again after a 409 or 404: see `createWriteController`. */
  run: <T>(
    key: string,
    call: () => Promise<T>,
    message: WriteMessage<T>,
  ) => Promise<WriteResult<T>>;
}

export interface UseAppActionsOptions {
  /** Reads the app (and what depends on it) again. */
  onChanged: () => void;
  /** The notice under the header and the live region; `null` clears it. */
  onNotice: (notice: WriteNotice | null) => void;
}

/**
 * The React side of `createWriteController`: it keeps the busy key in state so
 * the buttons re-render, and passes the controller the latest callbacks. Every
 * rule (single flight, the reload after a 409 or 404) is in the controller and
 * tested in `tests/unit/lib/app/writes.test.ts`.
 */
export function useAppActions({
  onChanged,
  onNotice,
}: UseAppActionsOptions): AppActions {
  const [busyKey, setBusyKey] = useState<string | null>(null);

  // The controller lives as long as the page; the callbacks are replaced after each render.
  const [controller] = useState(() =>
    createWriteController({
      reload: onChanged,
      setNotice: onNotice,
      setBusyKey,
    }),
  );
  useEffect(() => {
    controller.setEffects({
      reload: onChanged,
      setNotice: onNotice,
      setBusyKey,
    });
  }, [controller, onChanged, onNotice]);

  return {
    stateOf: (key) => writeStateOf(busyKey, key),
    run: controller.run,
  };
}
