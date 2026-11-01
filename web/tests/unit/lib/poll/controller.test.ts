import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { createPollController } from "@/lib/poll/controller";
import type { PollConfig, PollEnvironment } from "@/lib/poll/controller";
import { initialPollState, pollReducer } from "@/lib/poll/schedule";
import type { PollEvent, PollState } from "@/lib/poll/schedule";

const INTERVAL_MS = 5000;
const START_WALL_MS = Date.UTC(2026, 9, 30, 9, 0, 0);

/** A clock, a timer queue and a tab that the test moves by hand. */
class FakeEnvironment implements PollEnvironment {
  monotonicMs = 10_000;
  wallMs = START_WALL_MS;
  isVisible = true;
  readonly timers = new Map<number, { dueAtMs: number; run: () => void }>();
  readonly visibilityListeners = new Set<() => void>();
  private nextTimerId = 1;

  monotonicNowMs = (): number => this.monotonicMs;
  wallClockNowMs = (): number => this.wallMs;
  isTabVisible = (): boolean => this.isVisible;

  setTimer = (callback: () => void, delayMs: number): (() => void) => {
    const id = this.nextTimerId++;
    this.timers.set(id, { dueAtMs: this.monotonicMs + delayMs, run: callback });
    return () => {
      this.timers.delete(id);
    };
  };

  subscribeToVisibility = (listener: () => void): (() => void) => {
    this.visibilityListeners.add(listener);
    return () => {
      this.visibilityListeners.delete(listener);
    };
  };

  /** Moves both clocks forward, firing every timer that falls due on the way, earliest first. */
  advance(ms: number): void {
    const targetMs = this.monotonicMs + ms;
    for (;;) {
      let nextId: number | undefined;
      let nextDueMs = Infinity;
      for (const [id, timer] of this.timers) {
        if (timer.dueAtMs <= targetMs && timer.dueAtMs < nextDueMs) {
          nextId = id;
          nextDueMs = timer.dueAtMs;
        }
      }
      if (nextId === undefined) break;
      const timer = this.timers.get(nextId);
      this.timers.delete(nextId);
      this.wallMs += Math.max(0, nextDueMs - this.monotonicMs);
      this.monotonicMs = Math.max(this.monotonicMs, nextDueMs);
      timer?.run();
    }
    this.wallMs += targetMs - this.monotonicMs;
    this.monotonicMs = targetMs;
  }

  setVisible(isVisible: boolean): void {
    this.isVisible = isVisible;
    for (const listener of [...this.visibilityListeners]) listener();
  }
}

/** A loader whose calls stay open until the test settles them. */
class FakeLoader {
  readonly calls: {
    resolve: (value: string) => void;
    reject: (error: unknown) => void;
  }[] = [];
  /** The signal each call was given, in call order. */
  readonly signals: AbortSignal[] = [];
  inFlight = 0;
  maxInFlight = 0;
  throwSynchronously: Error | null = null;

  load = (signal: AbortSignal): Promise<string> => {
    if (this.throwSynchronously !== null) throw this.throwSynchronously;
    this.signals.push(signal);
    this.inFlight += 1;
    this.maxInFlight = Math.max(this.maxInFlight, this.inFlight);
    return new Promise<string>((resolve, reject) => {
      this.calls.push({
        resolve: (value) => {
          this.inFlight -= 1;
          resolve(value);
        },
        reject: (error) => {
          this.inFlight -= 1;
          reject(error instanceof Error ? error : new Error(String(error)));
        },
      });
    });
  };

  signalOf(index: number): AbortSignal {
    const signal = this.signals[index];
    if (!signal) throw new Error(`call ${index} was not given a signal`);
    return signal;
  }

  get callCount(): number {
    return this.calls.length;
  }

  succeed(index: number, value: string): void {
    this.calls[index]?.resolve(value);
  }

  fail(index: number, error: Error): void {
    this.calls[index]?.reject(error);
  }
}

/** Lets the settled promise chains run. */
async function settle(): Promise<void> {
  await new Promise<void>((resolve) => {
    setImmediate(resolve);
  });
}

function setup(config: Partial<PollConfig> = {}) {
  const environment = new FakeEnvironment();
  const loader = new FakeLoader();
  const events: PollEvent<string>[] = [];
  let state: PollState<string> = initialPollState<string>("");
  const controller = createPollController<string>({
    environment,
    load: loader.load,
    dispatch: (event) => {
      events.push(event);
      state = pollReducer(state, event);
    },
  });
  const fullConfig: PollConfig = {
    enabled: true,
    intervalMs: INTERVAL_MS,
    key: "",
    ...config,
  };
  return {
    environment,
    loader,
    events,
    controller,
    fullConfig,
    getState: () => state,
    start: () => {
      controller.configure(fullConfig);
    },
  };
}

describe("createPollController: the first call", () => {
  it("calls the loader at once and tells the page the call started", () => {
    const { loader, events, start } = setup({ key: "app:sales" });
    start();
    assert.equal(loader.callCount, 1);
    assert.deepEqual(events, [{ type: "started", key: "app:sales" }]);
  });

  it("does nothing before it is configured", () => {
    const { loader, environment, controller } = setup();
    controller.refresh();
    environment.advance(60_000);
    assert.equal(loader.callCount, 0);
    assert.equal(environment.timers.size, 0);
  });

  it("passes the answer on with the wall-clock time it arrived", async () => {
    const { loader, environment, events, start } = setup();
    start();
    environment.advance(2000);
    loader.succeed(0, "first");
    await settle();
    assert.deepEqual(events[1], {
      type: "succeeded",
      key: "",
      data: "first",
      atMs: START_WALL_MS + 2000,
    });
  });
});

describe("createPollController: the interval", () => {
  it("waits one interval after a call settles, not after it started", async () => {
    const { loader, environment, start } = setup();
    start();
    environment.advance(3000); // the call itself takes 3 s
    loader.succeed(0, "a");
    await settle();

    environment.advance(INTERVAL_MS - 1);
    assert.equal(loader.callCount, 1, "not yet");
    environment.advance(1);
    assert.equal(loader.callCount, 2, "one interval after the answer");
  });

  it("keeps polling after a failure", async () => {
    const { loader, environment, events, start } = setup();
    start();
    loader.fail(0, new Error("boom"));
    await settle();
    assert.equal(events[1]?.type, "failed");

    environment.advance(INTERVAL_MS);
    assert.equal(loader.callCount, 2);
  });

  it("treats a loader that throws before returning a promise as a failed call, and keeps polling", async () => {
    const { loader, environment, events, start } = setup();
    loader.throwSynchronously = new Error("not even started");
    start();
    await settle();
    assert.equal(loader.callCount, 0);
    assert.equal(events[0]?.type, "started");
    assert.equal(events[1]?.type, "failed");

    loader.throwSynchronously = null;
    environment.advance(INTERVAL_MS);
    assert.equal(loader.callCount, 1, "it tries again after the interval");
  });

  it("changing only the interval reschedules the wait without calling now", async () => {
    const { loader, environment, controller, fullConfig, start } = setup();
    start();
    loader.succeed(0, "a");
    await settle();

    environment.advance(1000);
    controller.configure({ ...fullConfig, intervalMs: 10_000 });
    assert.equal(loader.callCount, 1, "no call because the interval changed");

    environment.advance(8999);
    assert.equal(loader.callCount, 1);
    environment.advance(1);
    assert.equal(loader.callCount, 2, "10 s after the answer");
  });
});

describe("createPollController: calls never overlap", () => {
  it("starts no second call while one is in flight, whatever happens meanwhile", () => {
    const { loader, environment, controller, start } = setup();
    start();
    environment.advance(10 * INTERVAL_MS);
    controller.refresh();
    controller.refresh();
    environment.setVisible(false);
    environment.setVisible(true);
    assert.equal(loader.callCount, 1);
    assert.equal(loader.maxInFlight, 1);
  });

  it("runs a refresh asked for during a call right after that call settles, once", async () => {
    const { loader, controller, start } = setup();
    start();
    controller.refresh();
    controller.refresh();
    loader.succeed(0, "a");
    await settle();
    assert.equal(loader.callCount, 2, "exactly one extra call, at once");
    assert.equal(loader.maxInFlight, 1);
  });

  it("a refresh between two polls calls now and starts a new interval from its answer", async () => {
    const { loader, environment, controller, start } = setup();
    start();
    loader.succeed(0, "a");
    await settle();

    environment.advance(1000);
    controller.refresh();
    assert.equal(loader.callCount, 2);
    loader.succeed(1, "b");
    await settle();

    environment.advance(INTERVAL_MS - 1);
    assert.equal(loader.callCount, 2);
    environment.advance(1);
    assert.equal(loader.callCount, 3);
  });
});

describe("createPollController: a hidden tab", () => {
  it("calls nothing while the tab is hidden and calls at once when it shows", () => {
    const { loader, environment, start } = setup();
    environment.isVisible = false;
    start();
    environment.advance(60_000);
    assert.equal(loader.callCount, 0);

    environment.setVisible(true);
    assert.equal(loader.callCount, 1);
  });

  it("lets the timer lapse while hidden and calls at once on return when the interval has passed", async () => {
    const { loader, environment, start } = setup();
    start();
    loader.succeed(0, "a");
    await settle();

    environment.setVisible(false);
    environment.advance(60_000);
    assert.equal(loader.callCount, 1, "nothing while hidden");

    environment.setVisible(true);
    assert.equal(loader.callCount, 2, "overdue, so at once");
  });

  it("on return before the interval has passed waits only for the rest of it", async () => {
    const { loader, environment, start } = setup();
    start();
    loader.succeed(0, "a");
    await settle();

    environment.advance(1000);
    environment.setVisible(false);
    environment.setVisible(true);
    assert.equal(loader.callCount, 1);
    environment.advance(INTERVAL_MS - 1000 - 1);
    assert.equal(loader.callCount, 1);
    environment.advance(1);
    assert.equal(loader.callCount, 2);
  });

  it("still delivers the answer of a call that was in flight when the tab was hidden", async () => {
    const { loader, environment, getState, start } = setup();
    start();
    environment.setVisible(false);
    loader.succeed(0, "late");
    await settle();
    assert.equal(getState().result.status, "ready");
    environment.advance(60_000);
    assert.equal(loader.callCount, 1, "and still no new call while hidden");
  });
});

describe("createPollController: stop (the page unmounts)", () => {
  it("cancels the pending timer and the tab listener, so nothing is left behind", async () => {
    const { loader, environment, controller, start } = setup();
    start();
    loader.succeed(0, "a");
    await settle();
    assert.equal(environment.timers.size, 1);
    assert.equal(environment.visibilityListeners.size, 1);

    controller.stop();
    assert.equal(environment.timers.size, 0);
    assert.equal(environment.visibilityListeners.size, 0);
    environment.advance(60_000);
    assert.equal(loader.callCount, 1);
  });

  it("drops the answer of a call still on its way and starts nothing after it", async () => {
    const { loader, environment, events, controller, start } = setup();
    start();
    controller.stop();
    loader.succeed(0, "too late");
    await settle();

    assert.deepEqual(
      events.map((event) => event.type),
      ["started"],
      "no succeeded event after stop",
    );
    assert.equal(environment.timers.size, 0, "no timer scheduled after stop");
    assert.equal(loader.callCount, 1);
  });

  it("drops a failure that arrives after stop as well", async () => {
    const { loader, events, controller, start } = setup();
    start();
    controller.stop();
    loader.fail(0, new Error("late"));
    await settle();
    assert.deepEqual(
      events.map((event) => event.type),
      ["started"],
    );
  });

  it("ignores a tab event or a refresh after stop", () => {
    const { loader, environment, controller, start } = setup();
    start();
    controller.stop();
    environment.setVisible(false);
    environment.setVisible(true);
    controller.refresh();
    assert.equal(loader.callCount, 1);
  });

  it("can be configured again afterwards, as React does when it re-runs effects", async () => {
    const { loader, environment, controller, start } = setup();
    start();
    loader.succeed(0, "a");
    await settle();
    controller.stop();

    start();
    assert.equal(loader.callCount, 2, "a new start calls at once");
    assert.equal(environment.visibilityListeners.size, 1);
  });

  it("starts no second call while the one in flight at a stop is still open, and shows its answer if the same key is running again", async () => {
    // React StrictMode (development) stops and starts an effect at once.
    const { loader, environment, getState, controller, start } = setup();
    start();
    controller.stop();
    start();
    assert.equal(loader.callCount, 1, "the old call is still open");
    loader.succeed(0, "a");
    await settle();
    assert.equal(getState().result.status, "ready");
    assert.equal(loader.callCount, 1, "its answer is fresh: wait an interval");
    environment.advance(INTERVAL_MS);
    assert.equal(loader.callCount, 2);
    assert.equal(loader.maxInFlight, 1);
  });
});

describe("createPollController: enabled", () => {
  it("makes no call while disabled, and calls at once when enabled", () => {
    const { loader, environment, controller, fullConfig } = setup();
    controller.configure({ ...fullConfig, enabled: false });
    environment.advance(60_000);
    controller.refresh();
    assert.equal(loader.callCount, 0);
    assert.equal(environment.timers.size, 0);
    assert.equal(environment.visibilityListeners.size, 0);

    controller.configure(fullConfig);
    assert.equal(loader.callCount, 1);
  });

  it("stops the timer when it is disabled between two polls", async () => {
    const { loader, environment, controller, fullConfig, start } = setup();
    start();
    loader.succeed(0, "a");
    await settle();

    controller.configure({ ...fullConfig, enabled: false });
    assert.equal(environment.timers.size, 0);
    environment.advance(60_000);
    assert.equal(loader.callCount, 1);
  });

  it("still delivers the answer of the call in flight when it is disabled, so the page is not left 'fetching'", async () => {
    const { loader, controller, fullConfig, getState, start } = setup();
    start();
    controller.configure({ ...fullConfig, enabled: false });
    loader.succeed(0, "a");
    await settle();
    assert.equal(getState().isFetching, false);
    assert.equal(getState().result.status, "ready");
    assert.equal(loader.callCount, 1, "and no new call while disabled");
  });
});

describe("createPollController: the key", () => {
  it("names the key in every event of its calls", async () => {
    const { loader, events, start } = setup({ key: "app:sales" });
    start();
    loader.succeed(0, "a");
    await settle();
    assert.deepEqual(
      events.map((event) => event.key),
      ["app:sales", "app:sales"],
    );
  });

  it("loads the new key at once when it changes between two polls", async () => {
    const { loader, environment, controller, fullConfig, events, start } =
      setup({ key: "app:sales" });
    start();
    loader.succeed(0, "sales");
    await settle();

    environment.advance(1000);
    controller.configure({ ...fullConfig, key: "app:hr" });
    assert.equal(loader.callCount, 2);
    assert.deepEqual(events.at(-1), { type: "started", key: "app:hr" });
  });

  it("waits for the call in flight before it loads a changed key, so calls never overlap", async () => {
    const { loader, controller, fullConfig, events, start } = setup({
      key: "app:sales",
    });
    start();
    controller.configure({ ...fullConfig, key: "app:hr" });
    controller.configure({ ...fullConfig, key: "app:it" });
    assert.equal(loader.callCount, 1, "the old call is still open");

    loader.succeed(0, "sales");
    await settle();
    assert.equal(loader.callCount, 2, "the newest key loads right after");
    assert.equal(loader.maxInFlight, 1);
    assert.deepEqual(events.at(-1), { type: "started", key: "app:it" });
  });

  it("drops the answer of an old key, so it can never be shown for the new one", async () => {
    const { loader, controller, fullConfig, events, getState, start } = setup({
      key: "app:sales",
    });
    start();
    controller.configure({ ...fullConfig, key: "app:hr" });
    loader.succeed(0, "sales data");
    await settle();

    assert.ok(
      !events.some(
        (event) => event.type === "succeeded" && event.key !== "app:hr",
      ),
      "no event for the old key's answer",
    );
    loader.succeed(1, "hr data");
    await settle();
    const { result } = getState();
    assert.equal(result.status, "ready");
    if (result.status === "ready") assert.equal(result.data, "hr data");
  });

  it("starts the new key's interval from its own answer", async () => {
    const { loader, environment, controller, fullConfig, start } = setup({
      key: "app:sales",
    });
    start();
    controller.configure({ ...fullConfig, key: "app:hr" });
    loader.succeed(0, "sales");
    await settle();
    environment.advance(3000);
    loader.succeed(1, "hr");
    await settle();

    environment.advance(INTERVAL_MS - 1);
    assert.equal(loader.callCount, 2);
    environment.advance(1);
    assert.equal(loader.callCount, 3);
  });
});

describe("createPollController: cancelling a call", () => {
  const abortError = () => new DOMException("aborted", "AbortError");

  it("gives every call its own signal, open while the call runs", async () => {
    const { loader, environment, start } = setup();
    start();
    assert.equal(loader.signalOf(0).aborted, false);
    loader.succeed(0, "a");
    await settle();
    environment.advance(INTERVAL_MS);
    assert.equal(loader.callCount, 2);
    assert.notEqual(loader.signalOf(1), loader.signalOf(0));
    assert.equal(loader.signalOf(1).aborted, false);
  });

  it("stop() aborts the call in flight", () => {
    const { loader, controller, start } = setup();
    start();
    controller.stop();
    assert.equal(loader.signalOf(0).aborted, true);
  });

  it("a changed key aborts the call in flight, and loads the new key once that call has ended", async () => {
    const { loader, controller, fullConfig, events, start } = setup({
      key: "app:sales",
    });
    start();
    controller.configure({ ...fullConfig, key: "app:hr" });
    assert.equal(loader.signalOf(0).aborted, true);
    assert.equal(loader.callCount, 1, "calls still never overlap");

    loader.fail(0, abortError());
    await settle();
    assert.equal(loader.callCount, 2);
    assert.equal(loader.signalOf(1).aborted, false);
    assert.deepEqual(events.at(-1), { type: "started", key: "app:hr" });
  });

  it("an unchanged key and a mere pause leave the call alone", () => {
    const { loader, controller, fullConfig, start } = setup({ key: "k" });
    start();
    controller.configure({ ...fullConfig, intervalMs: 1000 });
    controller.configure({ ...fullConfig, enabled: false });
    assert.equal(loader.signalOf(0).aborted, false);
  });

  it("does not report the failure that is the abort, also when the same key runs again", async () => {
    const { loader, events, getState, controller, start } = setup();
    start();
    controller.stop();
    start();
    loader.fail(0, abortError());
    await settle();

    assert.ok(
      !events.some((event) => event.type === "failed"),
      "the cancellation is not news",
    );
    assert.equal(getState().result.status, "loading");
    assert.equal(loader.callCount, 2, "the loop carries on with a new call");
    assert.equal(loader.signalOf(1).aborted, false);
  });
});

describe("createPollController: the last good value", () => {
  it("keeps it on screen when a later call fails, and replaces it when one succeeds again", async () => {
    const { loader, environment, getState, start } = setup();
    start();
    loader.succeed(0, "good");
    await settle();
    assert.equal(getState().result.status, "ready");

    environment.advance(INTERVAL_MS);
    loader.fail(1, new Error("network down"));
    await settle();
    const stale = getState().result;
    assert.equal(stale.status, "stale");
    if (stale.status === "stale") {
      assert.equal(stale.data, "good");
      assert.equal((stale.error as Error).message, "network down");
    }
    assert.equal(getState().isFetching, false);

    environment.advance(INTERVAL_MS);
    loader.succeed(2, "better");
    await settle();
    const ready = getState().result;
    assert.equal(ready.status, "ready");
    if (ready.status === "ready") assert.equal(ready.data, "better");
  });

  it("has no data to keep when the very first call fails", async () => {
    const { loader, getState, start } = setup();
    start();
    loader.fail(0, new Error("denied"));
    await settle();
    assert.equal(getState().result.status, "failed");
  });
});
