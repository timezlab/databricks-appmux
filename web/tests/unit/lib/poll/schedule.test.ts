import assert from "node:assert/strict";
import { describe, it } from "vitest";

import {
  DEFAULT_POLL_INTERVAL_MS,
  MIN_POLL_INTERVAL_MS,
  initialPollState,
  nextPollStep,
  pollReducer,
  pollStateForKey,
} from "@/lib/poll/schedule";
import type { PollSchedulerInputs, PollState } from "@/lib/poll/schedule";

// ----------------------------------------------------------------- schedule

const INTERVAL_MS = 5000;

function inputs(
  overrides: Partial<PollSchedulerInputs> = {},
): PollSchedulerInputs {
  return {
    nowMs: 100_000,
    intervalMs: INTERVAL_MS,
    lastFinishedAtMs: 100_000,
    isCallInFlight: false,
    isTabVisible: true,
    isStopped: false,
    isRefreshRequested: false,
    ...overrides,
  };
}

describe("nextPollStep", () => {
  it("makes the first call at once", () => {
    assert.deepEqual(nextPollStep(inputs({ lastFinishedAtMs: null })), {
      action: "call",
    });
  });

  it("sleeps for the rest of the interval after a call finished", () => {
    assert.deepEqual(nextPollStep(inputs({ lastFinishedAtMs: 98_000 })), {
      action: "sleep",
      delayMs: 3000,
    });
    assert.deepEqual(nextPollStep(inputs({ lastFinishedAtMs: 100_000 })), {
      action: "sleep",
      delayMs: INTERVAL_MS,
    });
  });

  it("measures the interval from when the last call finished, so a slow call never makes two calls overlap or queue up", () => {
    // The call took 4 s: the next one is a full interval after it ended.
    const step = nextPollStep(
      inputs({ nowMs: 104_000, lastFinishedAtMs: 104_000 }),
    );
    assert.deepEqual(step, { action: "sleep", delayMs: INTERVAL_MS });
  });

  it("calls when the interval has passed, exactly or more", () => {
    assert.deepEqual(nextPollStep(inputs({ lastFinishedAtMs: 95_000 })), {
      action: "call",
    });
    assert.deepEqual(nextPollStep(inputs({ lastFinishedAtMs: 10_000 })), {
      action: "call",
    });
  });

  it("never starts a call while one is in flight, even when due or refreshed by hand", () => {
    for (const overrides of [
      { lastFinishedAtMs: null },
      { lastFinishedAtMs: 10_000 },
      { isRefreshRequested: true },
    ]) {
      assert.deepEqual(
        nextPollStep(inputs({ isCallInFlight: true, ...overrides })),
        { action: "idle" },
      );
    }
  });

  it("waits while the tab is hidden, however overdue, and a call follows when it shows again", () => {
    const hidden = inputs({ isTabVisible: false, lastFinishedAtMs: 10_000 });
    assert.deepEqual(nextPollStep(hidden), { action: "idle" });
    assert.deepEqual(nextPollStep({ ...hidden, lastFinishedAtMs: null }), {
      action: "idle",
    });
    assert.deepEqual(nextPollStep({ ...hidden, isRefreshRequested: true }), {
      action: "idle",
    });
    assert.deepEqual(nextPollStep({ ...hidden, isTabVisible: true }), {
      action: "call",
    });
  });

  it("does nothing once stopped, whatever else is true", () => {
    for (const overrides of [
      { lastFinishedAtMs: null },
      { lastFinishedAtMs: 10_000 },
      { isRefreshRequested: true },
    ]) {
      assert.deepEqual(
        nextPollStep(inputs({ isStopped: true, ...overrides })),
        {
          action: "idle",
        },
      );
    }
  });

  it("calls at once when a refresh was requested, ahead of the interval", () => {
    assert.deepEqual(nextPollStep(inputs({ isRefreshRequested: true })), {
      action: "call",
    });
  });

  it("never sleeps longer than one interval if the clock moved backwards", () => {
    assert.deepEqual(nextPollStep(inputs({ nowMs: 50_000 })), {
      action: "sleep",
      delayMs: INTERVAL_MS,
    });
  });

  it("will not poll faster than the minimum, whatever interval a page asks for", () => {
    for (const intervalMs of [0, -5, 1, Number.NaN]) {
      assert.deepEqual(nextPollStep(inputs({ intervalMs })), {
        action: "sleep",
        delayMs: MIN_POLL_INTERVAL_MS,
      });
    }
  });

  it("polls every few seconds by default", () => {
    assert.ok(DEFAULT_POLL_INTERVAL_MS >= 2000);
    assert.ok(DEFAULT_POLL_INTERVAL_MS <= 10_000);
    assert.ok(MIN_POLL_INTERVAL_MS <= DEFAULT_POLL_INTERVAL_MS);
  });
});

// ------------------------------------------------------------------ results

type Apps = { slug: string }[];
const KEY = "apps";
const FIRST: Apps = [{ slug: "sales" }];
const SECOND: Apps = [{ slug: "sales" }, { slug: "wiki" }];
const OFFLINE = new Error("offline");

function fresh(): PollState<Apps> {
  return initialPollState<Apps>(KEY);
}

describe("pollReducer", () => {
  it("starts loading, with nothing fetching yet", () => {
    assert.deepEqual(fresh(), {
      key: KEY,
      isFetching: false,
      result: { status: "loading" },
    });
  });

  it("marks a call in flight without touching the result", () => {
    const started = pollReducer(fresh(), { type: "started", key: KEY });
    assert.equal(started.isFetching, true);
    assert.deepEqual(started.result, { status: "loading" });
  });

  it("a first success makes the data ready and records when", () => {
    const state = pollReducer(
      pollReducer(fresh(), { type: "started", key: KEY }),
      { type: "succeeded", key: KEY, data: FIRST, atMs: 1000 },
    );
    assert.deepEqual(state.result, {
      status: "ready",
      data: FIRST,
      updatedAtMs: 1000,
    });
    assert.equal(state.isFetching, false);
  });

  it("a first failure is failed, with no data to fall back on", () => {
    const state = pollReducer(fresh(), {
      type: "failed",
      key: KEY,
      error: OFFLINE,
    });
    assert.deepEqual(state.result, { status: "failed", error: OFFLINE });
    assert.equal(state.isFetching, false);
  });

  it("keeps the last good value when a later poll fails, and says since when", () => {
    const ready = pollReducer(fresh(), {
      type: "succeeded",
      key: KEY,
      data: FIRST,
      atMs: 1000,
    });
    const stale = pollReducer(ready, {
      type: "failed",
      key: KEY,
      error: OFFLINE,
    });
    assert.deepEqual(stale.result, {
      status: "stale",
      data: FIRST,
      updatedAtMs: 1000,
      error: OFFLINE,
    });
  });

  it("keeps the data across several failures, and shows the latest error", () => {
    const other = new Error("503");
    let state = pollReducer(fresh(), {
      type: "succeeded",
      key: KEY,
      data: FIRST,
      atMs: 1000,
    });
    state = pollReducer(state, { type: "failed", key: KEY, error: OFFLINE });
    state = pollReducer(state, { type: "started", key: KEY });
    assert.equal(
      state.result.status,
      "stale",
      "a retry in flight still shows the error",
    );
    state = pollReducer(state, { type: "failed", key: KEY, error: other });
    assert.deepEqual(state.result, {
      status: "stale",
      data: FIRST,
      updatedAtMs: 1000,
      error: other,
    });
  });

  it("recovers: a success after a failure clears the error and replaces the data", () => {
    let state = pollReducer(fresh(), {
      type: "succeeded",
      key: KEY,
      data: FIRST,
      atMs: 1000,
    });
    state = pollReducer(state, { type: "failed", key: KEY, error: OFFLINE });
    state = pollReducer(state, {
      type: "succeeded",
      key: KEY,
      data: SECOND,
      atMs: 9000,
    });
    assert.deepEqual(state.result, {
      status: "ready",
      data: SECOND,
      updatedAtMs: 9000,
    });
  });

  it("a failure after only failures stays failed", () => {
    let state = pollReducer(fresh(), {
      type: "failed",
      key: KEY,
      error: OFFLINE,
    });
    state = pollReducer(state, { type: "failed", key: KEY, error: OFFLINE });
    assert.equal(state.result.status, "failed");
  });

  it("does not mutate the state it was given", () => {
    const before = fresh();
    const snapshot = structuredClone(before);
    pollReducer(before, { type: "started", key: KEY });
    assert.deepEqual(before, snapshot);
  });
});

describe("a change of key (another app, another query)", () => {
  const ready = pollReducer(initialPollState<Apps>("app:sales"), {
    type: "succeeded",
    key: "app:sales",
    data: FIRST,
    atMs: 1000,
  });

  it("is seen by the next render as loading, so the old key's data is never shown under the new one", () => {
    const seen = pollStateForKey(ready, "app:wiki");
    assert.deepEqual(seen.result, { status: "loading" });
    assert.equal(seen.key, "app:wiki");
  });

  it("is the same state when the key has not changed", () => {
    assert.equal(pollStateForKey(ready, "app:sales"), ready);
  });

  it("an event for the new key starts from loading, not from the old data", () => {
    const state = pollReducer(ready, {
      type: "failed",
      key: "app:wiki",
      error: OFFLINE,
    });
    assert.deepEqual(state.result, { status: "failed", error: OFFLINE });
    assert.equal(state.key, "app:wiki");
  });
});
