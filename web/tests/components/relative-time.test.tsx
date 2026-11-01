import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RelativeTime } from "@/components/relative-time";

// The shared ticker (clock-ticker.test.ts) is tested on its own; this checks
// that the component redraws on it and keeps the machine-readable parts.
const START_MS = Date.parse("2026-10-31T12:00:00Z");
const TICK_MS = 30_000;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
  vi.setSystemTime(START_MS);
});

afterEach(() => {
  vi.useRealTimers();
});

describe("RelativeTime", () => {
  it("is a <time> element with the machine-readable and the absolute time", () => {
    render(<RelativeTime value="2026-10-31T11:55:00Z" />);
    const time = screen.getByText(/ago/);
    expect(time.tagName).toBe("TIME");
    expect(time).toHaveAttribute("datetime", "2026-10-31T11:55:00.000Z");
    expect(time).toHaveAttribute("title", expect.stringContaining("2026"));
  });

  it("redraws on the shared tick, without a re-render from its parent", () => {
    render(<RelativeTime value="2026-10-31T11:59:30Z" />);
    const before = screen.getByRole("time").textContent;
    act(() => {
      vi.advanceTimersByTime(TICK_MS * 10);
    });
    expect(screen.getByRole("time").textContent).not.toBe(before);
  });

  it("says unknown, not a date, for no value", () => {
    render(<RelativeTime value={null} />);
    expect(screen.queryByRole("time")).toBeNull();
    expect(screen.getByText("unknown")).toBeInTheDocument();
  });
});
