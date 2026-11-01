import { useCallback, useEffect, useReducer, useRef } from "react";

import { createPollController } from "@/lib/poll/controller";
import type { PollController, PollEnvironment } from "@/lib/poll/controller";
import {
  DEFAULT_POLL_INTERVAL_MS,
  initialPollState,
  pollReducer,
  pollStateForKey,
} from "@/lib/poll/schedule";
import type { PollResult } from "@/lib/poll/schedule";

// The browser side of `PollEnvironment`. Every member reads `document`,
// `performance` or a timer only when called, never when this module loads, so
// the static prerender can import it.
const BROWSER_POLL_ENVIRONMENT: PollEnvironment = {
  monotonicNowMs: () => performance.now(),
  wallClockNowMs: () => Date.now(),
  setTimer: (callback, delayMs) => {
    const handle = setTimeout(callback, delayMs);
    return () => {
      clearTimeout(handle);
    };
  },
  isTabVisible: () => document.visibilityState === "visible",
  subscribeToVisibility: (listener) => {
    document.addEventListener("visibilitychange", listener);
    return () => {
      document.removeEventListener("visibilitychange", listener);
    };
  },
};

export interface UsePollOptions {
  /** Time from one call settling to the next call starting. Default 5 s, never below 1 s. */
  intervalMs?: number;
  /** `false` pauses the hook, the loader is not called; use it to wait for another answer (`GET /me`). Default `true`. */
  enabled?: boolean;
  /**
   * Names what the loader fetches (`"app:sales"`). When it changes, the data of
   * the old key leaves the screen at once and the loader runs again straight
   * away. A loader that depends on a slug, a window or a filter needs it.
   */
  resetKey?: string;
}

/**
 * `status` is `loading` (nothing yet), `ready` (the latest call succeeded),
 * `stale` (the latest call failed: `data` is the last good value, `error` why)
 * or `failed` (every call so far failed). `isFetching` is a call in flight.
 * `refresh()` asks for a call now, for after a write.
 */
export type UsePoll<T> = PollResult<T> & {
  isFetching: boolean;
  refresh: () => void;
};

/**
 * Calls `loader` now and then again every `intervalMs` after the previous call
 * settled, so two calls never overlap (also not when `resetKey` changes while
 * one is in flight: the new key loads right after it). Nothing is called while
 * the tab is hidden; showing it again calls at once if the interval has passed.
 * A failed call keeps the last good value (`status: "stale"`) and the next
 * interval tries again. Unmounting stops everything and drops an answer still
 * on its way.
 *
 * The loop is `createPollController` (lib/poll/controller.ts) and the rules for
 * when a call may start are `nextPollStep` (lib/poll/schedule.ts), both tested with
 * a fake clock and a fake tab; this hook only connects them to React and the
 * browser. `loader` may be an inline function: the latest one is called. It
 * gets an `AbortSignal` that aborts on unmount and when `resetKey` changes;
 * pass it to the API call (`getApp(slug, { signal })`) to cancel the request,
 * or ignore it, and the old answer is dropped as before.
 */
export function usePoll<T>(
  loader: (signal: AbortSignal) => Promise<T>,
  options: UsePollOptions = {},
): UsePoll<T> {
  const {
    intervalMs = DEFAULT_POLL_INTERVAL_MS,
    enabled = true,
    resetKey = "",
  } = options;

  const [held, dispatch] = useReducer(
    pollReducer<T>,
    resetKey,
    initialPollState<T>,
  );
  const state = pollStateForKey(held, resetKey);

  const loaderRef = useRef(loader);
  useEffect(() => {
    loaderRef.current = loader;
  }, [loader]);

  // Created in an effect, once, because it holds a ref to the loader: creating
  // it while rendering would read the ref during render.
  const controllerRef = useRef<PollController | null>(null);
  useEffect(() => {
    controllerRef.current ??= createPollController<T>({
      environment: BROWSER_POLL_ENVIRONMENT,
      load: (signal) => loaderRef.current(signal),
      dispatch,
    });
    controllerRef.current.configure({ enabled, intervalMs, key: resetKey });
  }, [enabled, intervalMs, resetKey]);

  // A separate effect so a change of options never stops the loop: only
  // unmounting does. React also runs this cleanup once in development
  // (StrictMode); the effect above then configures the same controller again.
  useEffect(
    () => () => {
      controllerRef.current?.stop();
    },
    [],
  );

  const refresh = useCallback(() => {
    controllerRef.current?.refresh();
  }, []);

  return { ...state.result, isFetching: state.isFetching, refresh };
}
