/**
 * The rules under `controller.ts`: when the next call may start, and what
 * the page sees after each call. The controller reads the clock, the tab and
 * the network, feeds them in here and does what comes back, so the rules (never
 * two calls at once, nothing while the tab is hidden, the last good value
 * survives a failed poll) are tested without a browser.
 */

/** "Every few seconds" (design 10.1). */
export const DEFAULT_POLL_INTERVAL_MS = 5000;
/** A page cannot ask for faster than this; the API and the registry behind it are shared by every open tab. */
export const MIN_POLL_INTERVAL_MS = 1000;

// ----------------------------------------------------------------- schedule

export interface PollSchedulerInputs {
  /** A monotonic clock (`performance.now()`), so a change of the system time cannot stall or flood the poll. */
  nowMs: number;
  intervalMs: number;
  /** When the last call settled, success or failure; `null` before the first call. */
  lastFinishedAtMs: number | null;
  isCallInFlight: boolean;
  isTabVisible: boolean;
  /** The hook was unmounted or disabled. */
  isStopped: boolean;
  /** The page asked for a call now (after a write, or a Refresh button). */
  isRefreshRequested: boolean;
}

/**
 * - `idle`: nothing to do until an event arrives (a call settles, the tab shows, a refresh is asked for);
 * - `call`: start a call now;
 * - `sleep`: look again after `delayMs`.
 */
export type PollStep =
  | { action: "idle" }
  | { action: "call" }
  | { action: "sleep"; delayMs: number };

const IDLE: PollStep = { action: "idle" };
const CALL: PollStep = { action: "call" };

export function nextPollStep(inputs: PollSchedulerInputs): PollStep {
  if (inputs.isStopped) return IDLE;
  // The settling call asks again; this is what keeps two calls from overlapping.
  if (inputs.isCallInFlight) return IDLE;
  // A hidden tab polls nothing; becoming visible asks again.
  if (!inputs.isTabVisible) return IDLE;
  if (inputs.isRefreshRequested) return CALL;
  if (inputs.lastFinishedAtMs === null) return CALL;

  const intervalMs = Number.isFinite(inputs.intervalMs)
    ? Math.max(inputs.intervalMs, MIN_POLL_INTERVAL_MS)
    : MIN_POLL_INTERVAL_MS;
  const elapsedMs = inputs.nowMs - inputs.lastFinishedAtMs;
  if (elapsedMs >= intervalMs) return CALL;
  // A clock that went backwards gives a negative elapsed time; one interval is the longest wait.
  return {
    action: "sleep",
    delayMs: Math.min(intervalMs, intervalMs - elapsedMs),
  };
}

// ------------------------------------------------------------------ results

/** What the page can show, as one value (no `data` and `error` flags to combine by hand). */
export type PollResult<T> =
  /** No call has settled yet. */
  | { status: "loading" }
  /** The latest call succeeded. */
  | { status: "ready"; data: T; updatedAtMs: number }
  /** The latest call failed; `data` is the last good value, received at `updatedAtMs`. */
  | { status: "stale"; data: T; updatedAtMs: number; error: unknown }
  /** Every call so far failed; there is no data. */
  | { status: "failed"; error: unknown };

export interface PollState<T> {
  /** What the data belongs to (see `usePoll`'s `resetKey`). */
  readonly key: string;
  /** A call is in flight. */
  readonly isFetching: boolean;
  readonly result: PollResult<T>;
}

export type PollEvent<T> =
  | { type: "started"; key: string }
  | { type: "succeeded"; key: string; data: T; atMs: number }
  | { type: "failed"; key: string; error: unknown };

export function initialPollState<T>(key: string): PollState<T> {
  return { key, isFetching: false, result: { status: "loading" } };
}

/**
 * The state a render for `key` should show. After the key changed (another app
 * slug, say) the old key's data is gone from the screen at once, before the
 * first answer for the new key arrives.
 */
export function pollStateForKey<T>(
  state: PollState<T>,
  key: string,
): PollState<T> {
  return state.key === key ? state : initialPollState<T>(key);
}

export function pollReducer<T>(
  held: PollState<T>,
  event: PollEvent<T>,
): PollState<T> {
  const state = pollStateForKey(held, event.key);
  switch (event.type) {
    case "started":
      return { ...state, isFetching: true };
    case "succeeded":
      return {
        key: state.key,
        isFetching: false,
        result: { status: "ready", data: event.data, updatedAtMs: event.atMs },
      };
    case "failed": {
      const { result } = state;
      const hasData = result.status === "ready" || result.status === "stale";
      return {
        key: state.key,
        isFetching: false,
        result: hasData
          ? {
              status: "stale",
              data: result.data,
              updatedAtMs: result.updatedAtMs,
              error: event.error,
            }
          : { status: "failed", error: event.error },
      };
    }
  }
}
