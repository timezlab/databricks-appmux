"use client";

import { useSyncExternalStore } from "react";

import { UNKNOWN_TEXT, describeTime } from "@/lib/format";
import type { TimeInput } from "@/lib/format";
import { createClockTicker } from "./clock-ticker";

// A relative time is wrong a minute later unless something redraws it. One
// shared timer ticks for every RelativeTime on the page (clock-ticker.ts).
const TICK_INTERVAL_MS = 30_000;

// The members read the browser only when called, so the static prerender can
// import this module.
const ticker = createClockTicker(TICK_INTERVAL_MS, {
  nowMs: () => Date.now(),
  setRepeatingTimer: (callback, intervalMs) => {
    const handle = setInterval(callback, intervalMs);
    return () => {
      clearInterval(handle);
    };
  },
});

// The prerendered HTML has no "now": it shows the absolute time and the browser
// swaps in the relative one, so the two never disagree during hydration.
function readServerTickCount(): null {
  return null;
}

export interface RelativeTimeProps {
  /** An ISO 8601 UTC string from the API (or epoch milliseconds). `null`/`undefined` reads "unknown". */
  value: TimeInput | null | undefined;
}

/**
 * "3 minutes ago" or "in 2 hours" in a `<time>` element whose hover title is
 * the absolute UTC time and whose `dateTime` is machine readable. Redraws every
 * 30 seconds. The time of "now" is read while rendering, not taken from the
 * last tick: a row that appears between two ticks reads "just now", not a time
 * in the future.
 */
export function RelativeTime({ value }: RelativeTimeProps) {
  // The tick count only makes React redraw; the clock is read below.
  const tickCount = useSyncExternalStore(
    ticker.subscribe,
    ticker.getTickCount,
    readServerTickCount,
  );
  const description = describeTime(
    value,
    tickCount === null ? null : ticker.nowMs(),
  );
  if (description === null) {
    return <span className="text-muted-foreground">{UNKNOWN_TEXT}</span>;
  }
  return (
    // A time is a short phrase ("21 hours ago"); splitting it over two lines reads as two facts.
    <time
      className="whitespace-nowrap"
      dateTime={description.iso}
      title={description.absolute}
    >
      {description.text}
    </time>
  );
}
