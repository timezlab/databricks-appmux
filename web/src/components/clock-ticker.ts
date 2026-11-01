/**
 * The shared timer behind `RelativeTime`. A relative time ("3 minutes ago") is
 * wrong a minute later unless something redraws it, so one timer ticks for every
 * `RelativeTime` on the page and stops when the last one unmounts.
 *
 * The ticker keeps no reading of the time. It only counts ticks (what
 * `useSyncExternalStore` compares to decide that a redraw is due) and answers
 * `nowMs()` from the clock at the moment it is asked. An earlier version cached
 * the reading taken at the last tick, up to one interval old, so a row that
 * appeared between ticks and was created seconds ago looked like it lay in the
 * future ("in 23 seconds").
 */

/** What the ticker needs from the browser. */
export interface TickerEnvironment {
  /** Epoch milliseconds (`Date.now()`). */
  nowMs: () => number;
  /** Runs `callback` every `intervalMs` until the returned function is called. */
  setRepeatingTimer: (callback: () => void, intervalMs: number) => () => void;
}

export interface ClockTicker {
  /** For `useSyncExternalStore`: calls `listener` on every tick; the returned function unsubscribes. */
  subscribe: (listener: () => void) => () => void;
  /** For `useSyncExternalStore`: changes on every tick and is the same value between two ticks. */
  getTickCount: () => number;
  /** The current time, read now. */
  nowMs: () => number;
}

export function createClockTicker(
  intervalMs: number,
  environment: TickerEnvironment,
): ClockTicker {
  const listeners = new Set<() => void>();
  let tickCount = 0;
  let stopTimer: (() => void) | null = null;

  function tick(): void {
    tickCount += 1;
    for (const notify of [...listeners]) notify();
  }

  return {
    subscribe(listener) {
      listeners.add(listener);
      stopTimer ??= environment.setRepeatingTimer(tick, intervalMs);
      return () => {
        listeners.delete(listener);
        if (listeners.size === 0) {
          stopTimer?.();
          stopTimer = null;
        }
      };
    },
    getTickCount: () => tickCount,
    nowMs: () => environment.nowMs(),
  };
}
