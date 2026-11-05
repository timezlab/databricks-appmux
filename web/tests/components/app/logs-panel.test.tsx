import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LogsPanel } from "@/components/app/logs/logs-panel";
import { LOG_POLL_INTERVAL_MS } from "@/lib/app/logs";
import { readExample } from "../../support/contract-examples";
import { jsonResponse, stubFetch } from "../../support/fake-fetch";
import type { RecordedCall } from "../../support/fake-fetch";

type Stream = "stdout" | "stderr" | "appmux";

function line(seq: number, stream: Stream, text: string) {
  return { seq, at: "2026-10-30T09:14:03Z", stream, text };
}

function logs(overrides: Record<string, unknown>) {
  return {
    state: "running",
    instance: "aaaa",
    lines: [],
    next_after: 0,
    dropped: 0,
    last_exit: null,
    ...overrides,
  };
}

let answers: (() => Response)[] = [];
let calls: URL[] = [];

function route({ url }: RecordedCall): Response {
  const parsed = new URL(url, "http://localhost");
  if (parsed.pathname.endsWith("/api/v1/apps/sales/logs")) {
    calls.push(parsed);
    const next = answers.length > 1 ? answers.shift() : answers[0];
    if (next !== undefined) return next();
  }
  return jsonResponse(404, {
    error: { code: "not_found", message: "no such route in this test" },
  });
}

async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

function setup(...pages: (() => Response)[]) {
  answers = pages;
  calls = [];
  stubFetch(route);
  render(<LogsPanel slug="sales" appName="Sales dashboard" />);
}

// fireEvent, not user-event: Testing Library's async wrapper waits on a
// setTimeout, which the fake clock of these tests holds, so user-event hangs.
function click(element: HTMLElement): void {
  act(() => {
    fireEvent.click(element);
  });
}

function logBox(): HTMLElement {
  return screen.getByRole("log", { name: "Output of Sales dashboard" });
}

function shownTexts(): string[] {
  return within(logBox())
    .queryAllByRole("listitem")
    .map((item) => item.textContent);
}

beforeEach(() => {
  vi.useFakeTimers({
    toFake: ["setTimeout", "clearTimeout", "Date", "performance"],
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("LogsPanel", () => {
  it("shows a loading status, then each line with its time, stream and text", async () => {
    setup(() => jsonResponse(200, readExample("AppLogs")));
    expect(screen.getByRole("status")).toHaveTextContent("Loading the logs");
    await advance(0);
    // The latest line is first.
    expect(shownTexts()).toEqual([
      "09:14:01appmuxexited with code 1 after 2.3 s",
      "09:14:01stderrTraceback (most recent call last):",
      "09:14:00appmuxstarted",
    ]);
    expect(calls[0]?.searchParams.get("after")).toBe("0");
  });

  it("asks for what came after the last answer every 2 seconds and appends it", async () => {
    setup(
      () =>
        jsonResponse(
          200,
          logs({ lines: [line(1, "stdout", "booting")], next_after: 1 }),
        ),
      () =>
        jsonResponse(
          200,
          logs({ lines: [line(2, "stdout", "listening")], next_after: 2 }),
        ),
    );
    await advance(0);
    await advance(LOG_POLL_INTERVAL_MS);
    expect(calls[1]?.searchParams.get("after")).toBe("1");
    // A line that arrives later goes above the ones before it.
    expect(shownTexts().map((text) => text.slice(14))).toEqual([
      "listening",
      "booting",
    ]);
  });

  it("clears the view when the app started again", async () => {
    setup(
      () =>
        jsonResponse(
          200,
          logs({ lines: [line(1, "stdout", "old start")], next_after: 1 }),
        ),
      () =>
        jsonResponse(
          200,
          logs({
            instance: "bbbb",
            lines: [line(2, "appmux", "started")],
            next_after: 2,
          }),
        ),
    );
    await advance(0);
    await advance(LOG_POLL_INTERVAL_MS);
    expect(shownTexts()).toEqual(["09:14:03appmuxstarted"]);
  });

  it("says how many earlier lines are not shown", async () => {
    setup(() =>
      jsonResponse(
        200,
        logs({ lines: [line(9, "stdout", "late")], next_after: 9, dropped: 8 }),
      ),
    );
    await advance(0);
    // The lines before the first one shown are below it, at the end of the list.
    expect(shownTexts()).toEqual([
      "09:14:03stdoutlate",
      "8 earlier lines are not shown",
    ]);
  });

  it("stops asking while paused and asks at once on Resume", async () => {
    setup(() =>
      jsonResponse(
        200,
        logs({ lines: [line(1, "stdout", "booting")], next_after: 1 }),
      ),
    );
    await advance(0);
    click(screen.getByRole("button", { name: "Pause" }));
    await advance(LOG_POLL_INTERVAL_MS * 3);
    expect(calls).toHaveLength(1);
    expect(screen.getByText("Paused")).toBeInTheDocument();

    click(screen.getByRole("button", { name: "Resume" }));
    await advance(0);
    expect(calls).toHaveLength(2);
  });

  it("shows only the lines of the chosen stream", async () => {
    setup(() => jsonResponse(200, readExample("AppLogs")));
    await advance(0);
    click(screen.getByRole("radio", { name: "stderr" }));
    expect(shownTexts()).toEqual([
      "09:14:01stderrTraceback (most recent call last):",
    ]);
  });

  it("says the chosen stream has no lines in the view, and how to see the rest", async () => {
    setup(() =>
      jsonResponse(
        200,
        logs({ lines: [line(1, "stdout", "booting")], next_after: 1 }),
      ),
    );
    await advance(0);
    click(screen.getByRole("radio", { name: "stderr" }));
    expect(shownTexts()).toEqual([]);
    expect(
      screen.getByText(
        "No stderr lines in the view. Choose All to see every line.",
      ),
    ).toBeInTheDocument();
  });

  it("turns Follow off when the person scrolls down to older lines, and Jump to latest turns it on", async () => {
    setup(() => jsonResponse(200, readExample("AppLogs")));
    await advance(0);
    const box = logBox();
    box.scrollTop = 100;
    box.dispatchEvent(new Event("scroll"));
    await advance(0);
    expect(screen.getByRole("button", { name: "Follow" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );

    click(screen.getByRole("button", { name: "Jump to latest" }));
    expect(screen.getByRole("button", { name: "Follow" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(box).toHaveFocus();
    // The latest line is at the top of the box.
    expect(box.scrollTop).toBe(0);
  });

  describe("search", () => {
    function searchBox(): HTMLElement {
      return screen.getByRole("searchbox", { name: "Search the lines" });
    }

    function search(text: string): void {
      act(() => {
        fireEvent.change(searchBox(), { target: { value: text } });
      });
    }

    it("shows only the lines whose text holds the words, whatever their case", async () => {
      setup(() => jsonResponse(200, readExample("AppLogs")));
      await advance(0);
      search("TRACEBACK");
      expect(shownTexts()).toEqual([
        "09:14:01stderrTraceback (most recent call last):",
      ]);
      expect(screen.getByText("1 of 3 lines match.")).toBeInTheDocument();
    });

    it("shows every line again when the search is cleared", async () => {
      setup(() => jsonResponse(200, readExample("AppLogs")));
      await advance(0);
      search("traceback");
      search("");
      expect(shownTexts()).toHaveLength(3);
      expect(screen.queryByText(/lines match\./)).not.toBeInTheDocument();
    });

    it("works together with the chosen stream", async () => {
      setup(() => jsonResponse(200, readExample("AppLogs")));
      await advance(0);
      click(screen.getByRole("radio", { name: "appmux" }));
      search("exited");
      expect(shownTexts()).toEqual([
        "09:14:01appmuxexited with code 1 after 2.3 s",
      ]);
    });

    it("says no line matches, with the words, and how to see the rest", async () => {
      setup(() => jsonResponse(200, readExample("AppLogs")));
      await advance(0);
      search("segfault");
      expect(shownTexts()).toEqual([]);
      expect(
        screen.getByText(
          /No lines match "segfault"\. Change or clear the search/,
        ),
      ).toBeInTheDocument();
    });

    it("keeps searching as new lines arrive, newest first", async () => {
      setup(
        () =>
          jsonResponse(
            200,
            logs({
              lines: [
                line(1, "stdout", "request ok"),
                line(2, "stdout", "request failed"),
              ],
              next_after: 2,
            }),
          ),
        () =>
          jsonResponse(
            200,
            logs({
              lines: [
                line(3, "stdout", "heartbeat"),
                line(4, "stdout", "request failed again"),
              ],
              next_after: 4,
            }),
          ),
      );
      await advance(0);
      search("failed");
      await advance(LOG_POLL_INTERVAL_MS);
      expect(shownTexts().map((text) => text.slice(14))).toEqual([
        "request failed again",
        "request failed",
      ]);
    });

    it("reads the words as text, never as a pattern", async () => {
      setup(() => jsonResponse(200, readExample("AppLogs")));
      await advance(0);
      search(".*");
      expect(shownTexts()).toEqual([]);
    });

    it("tells a screen reader how many lines match when the words change", async () => {
      setup(() => jsonResponse(200, readExample("AppLogs")));
      await advance(0);
      search("exited");
      expect(
        screen.getAllByRole("status").map((status) => status.textContent),
      ).toContain("1 line matches.");
    });
  });

  it("draws a line's text as text, never as markup", async () => {
    setup(() =>
      jsonResponse(
        200,
        logs({
          lines: [line(1, "stdout", "<img src=x onerror=alert(1)>")],
          next_after: 1,
        }),
      ),
    );
    await advance(0);
    expect(logBox().querySelector("img")).toBeNull();
    expect(
      within(logBox()).getByText("<img src=x onerror=alert(1)>"),
    ).toBeInTheDocument();
  });

  it("shows how the last process ended when the app is not running", async () => {
    setup(() => jsonResponse(200, readExample("AppLogs")));
    await advance(0);
    expect(
      screen.getByText(/Last exit: Exited with code 1 after 2.3 s/),
    ).toBeInTheDocument();
    expect(screen.getByText("The app crashed")).toBeInTheDocument();
  });

  it("says appmux has not started the app when there is no output yet", async () => {
    setup(() =>
      jsonResponse(200, logs({ state: "never_started", instance: null })),
    );
    await advance(0);
    expect(
      screen.getByText(
        /appmux has not started this app since it started itself/,
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("log")).not.toBeInTheDocument();
  });

  it("says the app printed nothing when it runs with no lines", async () => {
    setup(() => jsonResponse(200, logs({})));
    await advance(0);
    expect(screen.getByText("Nothing printed yet")).toBeInTheDocument();
  });

  it("says the logs could not be loaded and offers to try again", async () => {
    setup(() =>
      jsonResponse(503, {
        error: { code: "unavailable", message: "try later" },
      }),
    );
    await advance(0);
    expect(screen.getByText(/Could not load the logs/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /try again/i })).toBeVisible();
  });
});
