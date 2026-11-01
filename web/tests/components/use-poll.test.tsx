import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { usePoll } from "@/hooks/use-poll";

// The loop and its rules are tested with a fake clock in tests/unit/lib/poll;
// these tests check that the hook connects them to React and to the browser
// (timers, the visibility of the tab, unmounting).

const INTERVAL_MS = 5000;

async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

function setTabVisibility(state: "visible" | "hidden"): void {
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => state,
  });
  document.dispatchEvent(new Event("visibilitychange"));
}

beforeEach(() => {
  vi.useFakeTimers({
    toFake: ["setTimeout", "clearTimeout", "Date", "performance"],
  });
});

afterEach(() => {
  setTabVisibility("visible");
  vi.useRealTimers();
});

describe("usePoll", () => {
  it("is loading, then ready with what the loader returned", async () => {
    const loader = vi.fn(() => Promise.resolve("first"));
    const { result } = renderHook(() => usePoll(loader));
    expect(result.current.status).toBe("loading");
    await advance(0);
    expect(result.current).toMatchObject({ status: "ready", data: "first" });
    expect(loader).toHaveBeenCalledTimes(1);
  });

  it("calls again one interval after the previous call settled", async () => {
    const loader = vi
      .fn<() => Promise<string>>()
      .mockResolvedValueOnce("first")
      .mockResolvedValueOnce("second");
    const { result } = renderHook(() =>
      usePoll(loader, { intervalMs: INTERVAL_MS }),
    );
    await advance(0);
    await advance(INTERVAL_MS - 1);
    expect(loader).toHaveBeenCalledTimes(1);
    await advance(1);
    expect(loader).toHaveBeenCalledTimes(2);
    expect(result.current).toMatchObject({ status: "ready", data: "second" });
  });

  it("keeps the last good value as stale when a later call fails", async () => {
    const failure = new Error("offline");
    const loader = vi
      .fn<() => Promise<string>>()
      .mockResolvedValueOnce("good")
      .mockRejectedValueOnce(failure);
    const { result } = renderHook(() =>
      usePoll(loader, { intervalMs: INTERVAL_MS }),
    );
    await advance(0);
    await advance(INTERVAL_MS);
    expect(result.current).toMatchObject({
      status: "stale",
      data: "good",
      error: failure,
    });
  });

  it("refresh() calls the loader now, not at the next interval", async () => {
    const loader = vi.fn(() => Promise.resolve("value"));
    const { result } = renderHook(() =>
      usePoll(loader, { intervalMs: INTERVAL_MS }),
    );
    await advance(0);
    act(() => {
      result.current.refresh();
    });
    await advance(0);
    expect(loader).toHaveBeenCalledTimes(2);
  });

  it("calls nothing while disabled, and starts when enabled", async () => {
    const loader = vi.fn(() => Promise.resolve("value"));
    const { result, rerender } = renderHook(
      ({ enabled }) => usePoll(loader, { enabled }),
      { initialProps: { enabled: false } },
    );
    await advance(INTERVAL_MS * 2);
    expect(loader).not.toHaveBeenCalled();
    expect(result.current.status).toBe("loading");
    rerender({ enabled: true });
    await advance(0);
    expect(result.current.status).toBe("ready");
  });

  it("drops the data of the old resetKey at once and loads the new one", async () => {
    let calls = 0;
    const loader = vi.fn(() => {
      calls += 1;
      return Promise.resolve(`value ${calls}`);
    });
    const { result, rerender } = renderHook(
      ({ resetKey }) => usePoll(loader, { resetKey }),
      { initialProps: { resetKey: "app:sales" } },
    );
    await advance(0);
    expect(result.current).toMatchObject({ status: "ready", data: "value 1" });
    rerender({ resetKey: "app:ledger" });
    expect(result.current.status).toBe("loading");
    await advance(0);
    expect(result.current).toMatchObject({ status: "ready", data: "value 2" });
  });

  it("makes no call while the tab is hidden and calls when it is shown again", async () => {
    const loader = vi.fn(() => Promise.resolve("value"));
    renderHook(() => usePoll(loader, { intervalMs: INTERVAL_MS }));
    await advance(0);
    setTabVisibility("hidden");
    await advance(INTERVAL_MS * 3);
    expect(loader).toHaveBeenCalledTimes(1);
    setTabVisibility("visible");
    await advance(0);
    expect(loader).toHaveBeenCalledTimes(2);
  });

  it("stops when it unmounts", async () => {
    const loader = vi.fn(() => Promise.resolve("value"));
    const { unmount } = renderHook(() =>
      usePoll(loader, { intervalMs: INTERVAL_MS }),
    );
    await advance(0);
    unmount();
    await advance(INTERVAL_MS * 3);
    expect(loader).toHaveBeenCalledTimes(1);
  });
});
