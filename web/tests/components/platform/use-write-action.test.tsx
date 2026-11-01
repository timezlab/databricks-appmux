import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { PlatformProvider } from "@/components/platform/platform-context";
import { useWriteAction } from "@/components/platform/use-write-action";
import type { ActionOutcome } from "@/lib/platform/action";

const SAVED: ActionOutcome = { tone: "success", title: "Saved sales" };

function setup() {
  const refresh = vi.fn();
  const announce = vi.fn();
  const hook = renderHook(() => useWriteAction(), {
    wrapper: ({ children }: { children: ReactNode }) => (
      <PlatformProvider value={{ refresh, announce }}>
        {children}
      </PlatformProvider>
    ),
  });
  return { ...hook, refresh, announce };
}

/** A write the test finishes by hand. */
function pendingWrite() {
  let succeed: (outcome: ActionOutcome) => void = () => undefined;
  let fail: (error: unknown) => void = () => undefined;
  const perform = vi.fn(
    () =>
      new Promise<ActionOutcome>((resolve, reject) => {
        succeed = resolve;
        fail = reject;
      }),
  );
  return {
    perform,
    succeed: (outcome: ActionOutcome) => {
      succeed(outcome);
    },
    fail: (error: unknown) => {
      fail(error);
    },
  };
}

describe("useWriteAction", () => {
  it("is idle, then busy while the write runs, then done with the outcome", async () => {
    const { result, announce, refresh } = setup();
    const write = pendingWrite();
    expect(result.current.state).toEqual({ status: "idle" });

    let outcome: Promise<boolean> = Promise.resolve(false);
    act(() => {
      outcome = result.current.run(write.perform);
    });
    expect(result.current.isBusy).toBe(true);
    expect(result.current.state).toEqual({ status: "busy" });
    expect(announce).not.toHaveBeenCalled();

    await act(async () => {
      write.succeed(SAVED);
      await outcome;
    });
    await expect(outcome).resolves.toBe(true);
    expect(result.current.isBusy).toBe(false);
    expect(result.current.state).toEqual({ status: "done", outcome: SAVED });
    // A success tells screen readers and asks the page for fresh data, once each.
    expect(announce).toHaveBeenCalledExactlyOnceWith("Saved sales");
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("keeps the thrown error as the failure and tells the page nothing", async () => {
    const { result, announce, refresh } = setup();
    const error = new Error("boom");
    let outcome: Promise<boolean> = Promise.resolve(true);
    await act(async () => {
      outcome = result.current.run(() => Promise.reject(error));
      await outcome;
    });
    await expect(outcome).resolves.toBe(false);
    expect(result.current.isBusy).toBe(false);
    expect(result.current.state).toEqual({ status: "failed", error });
    expect(announce).not.toHaveBeenCalled();
    expect(refresh).not.toHaveBeenCalled();
  });

  it("ignores a second write while one is in flight", async () => {
    const { result } = setup();
    const first = pendingWrite();
    const second = pendingWrite();
    let firstOutcome: Promise<boolean> = Promise.resolve(false);
    let secondOutcome: Promise<boolean> = Promise.resolve(true);
    act(() => {
      firstOutcome = result.current.run(first.perform);
      secondOutcome = result.current.run(second.perform);
    });
    await expect(secondOutcome).resolves.toBe(false);
    expect(second.perform).not.toHaveBeenCalled();
    expect(first.perform).toHaveBeenCalledTimes(1);

    await act(async () => {
      first.succeed(SAVED);
      await firstOutcome;
    });
    expect(result.current.state).toEqual({ status: "done", outcome: SAVED });
  });

  it("retry repeats the last write, and does nothing before any write", async () => {
    const { result, announce } = setup();
    act(() => {
      result.current.retry();
    });
    expect(result.current.state).toEqual({ status: "idle" });

    const perform = vi
      .fn<() => Promise<ActionOutcome>>()
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce(SAVED);
    await act(async () => {
      await result.current.run(perform);
    });
    expect(result.current.state.status).toBe("failed");

    await act(async () => {
      result.current.retry();
      await Promise.resolve();
    });
    expect(perform).toHaveBeenCalledTimes(2);
    expect(result.current.state).toEqual({ status: "done", outcome: SAVED });
    expect(announce).toHaveBeenCalledExactlyOnceWith("Saved sales");
  });

  it("clear hides a finished result", async () => {
    const { result } = setup();
    await act(async () => {
      await result.current.run(() => Promise.resolve(SAVED));
    });
    expect(result.current.state.status).toBe("done");
    act(() => {
      result.current.clear();
    });
    expect(result.current.state).toEqual({ status: "idle" });
  });
});
