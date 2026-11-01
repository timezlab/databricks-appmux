/**
 * The loop behind `usePoll`, without React: it starts a call, waits, starts the
 * next, and tells the page what happened. Everything it touches from the
 * outside (the clocks, the timer, the tab, the loader, the page's `dispatch`)
 * is handed in, so the rules run under test with a fake clock and a fake tab:
 *  - calls never overlap, also not when the key changes or the hook is
 *    stopped and started again while a call is in flight;
 *  - nothing is called while the tab is hidden;
 *  - `stop()` leaves no timer and no listener, and the answer of a call still
 *    on its way is dropped;
 *  - every call gets an `AbortSignal`; `stop()` and a changed key abort the
 *    call in flight, and the failure that abort causes is not reported;
 *  - a failed call is reported as an event; the reducer in `schedule.ts`
 *    keeps the last good value.
 * When the next call may start is decided by `nextPollStep`, not here.
 */
import { nextPollStep } from "./schedule";
import type { PollEvent } from "./schedule";

/** What the controller needs from the browser. */
export interface PollEnvironment {
  /** A monotonic clock (`performance.now()`); a change of the system time must not stall or flood the poll. */
  monotonicNowMs: () => number;
  /** Epoch milliseconds (`Date.now()`), for "updated at". */
  wallClockNowMs: () => number;
  /** Runs `callback` once after `delayMs`; the returned function cancels it. */
  setTimer: (callback: () => void, delayMs: number) => () => void;
  isTabVisible: () => boolean;
  /** Calls `listener` whenever the tab is shown or hidden; the returned function unsubscribes. */
  subscribeToVisibility: (listener: () => void) => () => void;
}

export interface PollConfig {
  /** `false` pauses the loop: no call is made, no timer and no listener is held. */
  enabled: boolean;
  /** Time from one call settling to the next call starting. */
  intervalMs: number;
  /** Names what the loader fetches (see `usePoll`'s `resetKey`). */
  key: string;
}

export interface PollController {
  /**
   * Applies the configuration. The first call, or one after `stop()` or after
   * the loop was disabled, starts it: the loader is called at once. A changed
   * `key` loads the new key at once (after the call in flight, if any). A
   * changed `intervalMs` only changes the wait.
   */
  configure: (config: PollConfig) => void;
  /** Asks for a call now (after a write). Ignored while stopped or disabled. */
  refresh: () => void;
  /** Cancels the timer and the tab listener and drops the answer of a call in flight. `configure` may start it again. */
  stop: () => void;
}

export interface PollControllerOptions<T> {
  environment: PollEnvironment;
  /**
   * One call. It may throw or reject; either is a failed call. `signal` aborts
   * when the page no longer wants the answer (`stop()`, a changed key): pass it
   * on to `fetch`; a loader that ignores it still works, its answer is then
   * dropped as before.
   */
  load: (signal: AbortSignal) => Promise<T>;
  /** The page's `useReducer` dispatch. */
  dispatch: (event: PollEvent<T>) => void;
}

export function createPollController<T>(
  options: PollControllerOptions<T>,
): PollController {
  const { environment, load, dispatch } = options;

  /** `null` before the first `configure` and after `stop`. */
  let config: PollConfig | null = null;
  let isCallInFlight = false;
  let isRefreshRequested = false;
  /** The call in flight, so `stop()` and a changed key can cancel it; `null` between calls. */
  let inFlight: AbortController | null = null;
  let lastFinishedAtMs: number | null = null;
  let cancelTimer: (() => void) | null = null;
  let unsubscribeFromVisibility: (() => void) | null = null;

  function clearTimer(): void {
    cancelTimer?.();
    cancelTimer = null;
  }

  function step(): void {
    clearTimer();
    const next = nextPollStep({
      nowMs: environment.monotonicNowMs(),
      intervalMs: config?.intervalMs ?? 0,
      lastFinishedAtMs,
      isCallInFlight,
      isTabVisible: environment.isTabVisible(),
      isStopped: !config?.enabled,
      isRefreshRequested,
    });
    if (next.action === "sleep") {
      cancelTimer = environment.setTimer(step, next.delayMs);
    } else if (next.action === "call" && config !== null) {
      // runCall never rejects: it turns the loader's failure into a "failed" event.
      void runCall(config.key);
    }
  }

  async function runCall(key: string): Promise<void> {
    isCallInFlight = true;
    isRefreshRequested = false;
    const cancellation = new AbortController();
    inFlight = cancellation;
    dispatch({ type: "started", key });
    let outcome: PollEvent<T> | null;
    try {
      const data = await load(cancellation.signal);
      outcome = {
        type: "succeeded",
        key,
        data,
        atMs: environment.wallClockNowMs(),
      };
    } catch (error) {
      // A call this controller aborted fails with the abort: that is the
      // page's own doing, not an error to show. A failure that came before the
      // abort or without one is still reported.
      outcome = cancellation.signal.aborted
        ? null
        : { type: "failed", key, error };
    }
    isCallInFlight = false;
    inFlight = null;
    // An answer is shown only while the loop runs and still wants this key.
    // After `stop()` the page is gone; after a key change the answer belongs to
    // something else, and the new key is loaded next (`lastFinishedAtMs` is null).
    // A merely disabled loop still delivers: the data is right for the key
    // and the page must not stay "fetching".
    if (outcome !== null && config?.key === key) {
      lastFinishedAtMs = environment.monotonicNowMs();
      dispatch(outcome);
    }
    step();
  }

  function begin(): void {
    lastFinishedAtMs = null;
    isRefreshRequested = false;
    unsubscribeFromVisibility ??= environment.subscribeToVisibility(step);
  }

  function pause(): void {
    clearTimer();
    unsubscribeFromVisibility?.();
    unsubscribeFromVisibility = null;
    isRefreshRequested = false;
  }

  return {
    configure(next) {
      const previous = config;
      config = next;
      // Its answer would be dropped anyway; cancelling frees the connection and
      // lets the new key load as soon as the aborted call has ended.
      if (previous !== null && previous.key !== next.key) inFlight?.abort();
      if (!next.enabled) {
        pause();
        return;
      }
      if (!previous?.enabled) {
        begin();
      } else if (previous.key !== next.key) {
        // The new key has no answer yet: load it as soon as no call is open.
        lastFinishedAtMs = null;
      }
      step();
    },

    refresh() {
      if (!config?.enabled) return;
      isRefreshRequested = true;
      step();
    },

    stop() {
      config = null;
      inFlight?.abort();
      pause();
    },
  };
}
