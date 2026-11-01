import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { createClockTicker } from "@/components/clock-ticker";
import type { TickerEnvironment } from "@/components/clock-ticker";

const TICK_MS = 30_000;

/** A clock and one repeating timer slot that the test drives by hand. */
function fakeEnvironment() {
  let nowMs = 1_000_000;
  const timers = new Set<{ run: () => void; intervalMs: number }>();
  const environment: TickerEnvironment = {
    nowMs: () => nowMs,
    setRepeatingTimer: (callback, intervalMs) => {
      const timer = { run: callback, intervalMs };
      timers.add(timer);
      return () => {
        timers.delete(timer);
      };
    },
  };
  return {
    environment,
    timers,
    advanceClock(ms: number) {
      nowMs += ms;
    },
    /** One interval of the timer passes. */
    fireTimers() {
      nowMs += TICK_MS;
      for (const timer of [...timers]) timer.run();
    },
  };
}

describe("createClockTicker", () => {
  it("runs no timer until somebody listens, and one timer however many listen", () => {
    const fake = fakeEnvironment();
    const ticker = createClockTicker(TICK_MS, fake.environment);
    assert.equal(fake.timers.size, 0);

    const stopFirst = ticker.subscribe(() => undefined);
    const stopSecond = ticker.subscribe(() => undefined);
    assert.equal(fake.timers.size, 1);
    assert.equal([...fake.timers][0]?.intervalMs, TICK_MS);

    stopFirst();
    assert.equal(fake.timers.size, 1, "one listener is left");
    stopSecond();
    assert.equal(fake.timers.size, 0, "the last one to leave stops the timer");
  });

  it("starts a new timer when somebody listens again", () => {
    const fake = fakeEnvironment();
    const ticker = createClockTicker(TICK_MS, fake.environment);
    ticker.subscribe(() => undefined)();
    ticker.subscribe(() => undefined);
    assert.equal(fake.timers.size, 1);
  });

  it("notifies every listener on each tick and changes the tick count", () => {
    const fake = fakeEnvironment();
    const ticker = createClockTicker(TICK_MS, fake.environment);
    let first = 0;
    let second = 0;
    ticker.subscribe(() => (first += 1));
    ticker.subscribe(() => (second += 1));
    const before = ticker.getTickCount();

    fake.fireTimers();
    assert.deepEqual([first, second], [1, 1]);
    assert.notEqual(ticker.getTickCount(), before);

    fake.fireTimers();
    assert.deepEqual([first, second], [2, 2]);
  });

  it("returns the same tick count until the next tick, as useSyncExternalStore requires", () => {
    const fake = fakeEnvironment();
    const ticker = createClockTicker(TICK_MS, fake.environment);
    ticker.subscribe(() => undefined);
    const count = ticker.getTickCount();
    fake.advanceClock(20_000);
    assert.equal(ticker.getTickCount(), count);
    assert.equal(ticker.getTickCount(), count);
  });

  it("does not notify a listener that has left", () => {
    const fake = fakeEnvironment();
    const ticker = createClockTicker(TICK_MS, fake.environment);
    let leaver = 0;
    let stayer = 0;
    const leave = ticker.subscribe(() => (leaver += 1));
    ticker.subscribe(() => (stayer += 1));
    leave();
    leave(); // leaving twice is harmless
    fake.fireTimers();
    assert.deepEqual([leaver, stayer], [0, 1]);
    assert.equal(fake.timers.size, 1);
  });

  it("reads the current time on every call, never the time of the last tick", () => {
    // The bug this guards: a cached reading up to one interval old made a row
    // created seconds ago read "in 23 seconds" (a time in the future).
    const fake = fakeEnvironment();
    const ticker = createClockTicker(TICK_MS, fake.environment);
    ticker.subscribe(() => undefined);
    fake.fireTimers();
    const atTick = ticker.nowMs();

    fake.advanceClock(25_000);
    assert.equal(ticker.nowMs(), atTick + 25_000);
  });

  it("reads the current time also when nobody listens", () => {
    const fake = fakeEnvironment();
    const ticker = createClockTicker(TICK_MS, fake.environment);
    const first = ticker.nowMs();
    fake.advanceClock(7000);
    assert.equal(ticker.nowMs(), first + 7000);
  });
});
