/**
 * How the app page runs its writes: one at a time, and a read of the app again
 * when the answer says the page was stale. The rules live here, not in the
 * React hook, so they are tested with plain functions; the hook only keeps the
 * busy key in state and hands the controller its three effects.
 */
import { shouldReloadAfterError } from "./actions";

/** A sentence for the notice under the header and for the live region. */
export interface WriteNotice {
  /** `success` after a write; `info` when the page read the app again by itself. */
  tone: "success" | "info";
  text: string;
}

export type WriteResult<T> =
  { ok: true; value: T } | { ok: false; error: unknown };

/** `busy` is the write in flight; `blocked` is every other one meanwhile. */
export type WriteState = "idle" | "busy" | "blocked";

/** The sentence after success; a function when it depends on what the server answered. */
export type WriteMessage<T> = string | ((value: T) => string);

export const RELOAD_NOTICE_TEXT =
  "The page read the app again because the server holds something different now (changed by someone else, or gone). Check what you see, then try again.";

/** What `run` hands back when another write is still running; its call is never made. */
export class WriteBusyError extends Error {
  constructor() {
    super("Another change is still running; wait for it to finish.");
    this.name = "WriteBusyError";
  }
}

export interface WriteControllerEffects {
  /** Reads the app (and what depends on it) again. */
  reload: () => void;
  /** The notice under the header and the live region; `null` clears it. */
  setNotice: (notice: WriteNotice | null) => void;
  /** The key of the write in flight, or `null` when none is. */
  setBusyKey: (key: string | null) => void;
}

export interface WriteController {
  /**
   * Replaces the effects `run` calls from now on, including for a write
   * already in flight. A React page passes new callbacks on every render; the
   * controller (and the one-write-at-a-time guard it keeps) must outlive them.
   */
  setEffects: (effects: WriteControllerEffects) => void;
  /**
   * Runs one write. Only one runs at a time on the page: two writes to the
   * same app racing each other (activate and delete) is worse than a short
   * wait, so a second one is refused with a `WriteBusyError` without being
   * called. On success it announces `message` and reads the app again. On
   * failure it hands the error back so the caller can show it where the
   * person is looking; a 409 (the page was stale) or a 404 (the thing is gone)
   * also reads the app again, once, and says so.
   */
  run: <T>(
    key: string,
    call: () => Promise<T>,
    message: WriteMessage<T>,
  ) => Promise<WriteResult<T>>;
}

export function writeStateOf(busyKey: string | null, key: string): WriteState {
  if (busyKey === null) return "idle";
  return busyKey === key ? "busy" : "blocked";
}

export function createWriteController(
  initialEffects: WriteControllerEffects,
): WriteController {
  let effects = initialEffects;
  // The page's state lags a render; this refuses a second press in the same tick.
  let busyKey: string | null = null;

  async function run<T>(
    key: string,
    call: () => Promise<T>,
    message: WriteMessage<T>,
  ): Promise<WriteResult<T>> {
    if (busyKey !== null) return { ok: false, error: new WriteBusyError() };
    busyKey = key;
    effects.setBusyKey(key);
    effects.setNotice(null);
    try {
      const value = await call();
      effects.setNotice({
        tone: "success",
        text: typeof message === "string" ? message : message(value),
      });
      effects.reload();
      return { ok: true, value };
    } catch (error) {
      // Returned to the caller, which shows it; the reload is the only handling here.
      if (shouldReloadAfterError(error)) {
        effects.reload();
        effects.setNotice({ tone: "info", text: RELOAD_NOTICE_TEXT });
      }
      return { ok: false, error };
    } finally {
      busyKey = null;
      effects.setBusyKey(null);
    }
  }

  function setEffects(next: WriteControllerEffects): void {
    effects = next;
  }

  return { run, setEffects };
}
