import assert from "node:assert/strict";
import { describe, it } from "vitest";

import * as api from "@/lib/api";
import {
  EMPTY_LOG_VIEW,
  LOG_ENTRIES_KEPT_MAX,
  describeLastExit,
  filterLogEntries,
  gapText,
  isScrolledToBottom,
  logCopyText,
  logLineTime,
  mergeLogPage,
} from "@/lib/app/logs";
import type { LogEntry, LogView } from "@/lib/app/logs";
import { readExample } from "../../../support/contract-examples";

const EXAMPLE_LOGS = api.AppLogs.parse(readExample("AppLogs"));

type Stream = api.AppLogs["lines"][number]["stream"];

function line(seq: number, stream: Stream = "stdout", text = `line ${seq}`) {
  return { seq, at: "2026-10-30T09:14:03Z", stream, text };
}

function page(overrides: Partial<api.AppLogs>): api.AppLogs {
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

function seqs(view: LogView): (number | string)[] {
  return view.entries.map((entry) =>
    entry.kind === "line" ? entry.seq : `gap:${entry.count}`,
  );
}

describe("mergeLogPage", () => {
  it("takes the first page whole: its lines, instance, cursor, state and last exit", () => {
    const view = mergeLogPage(EMPTY_LOG_VIEW, EXAMPLE_LOGS);
    assert.deepEqual(seqs(view), [41, 42, 43]);
    assert.equal(view.instance, "9f2c4e1a7b3d5f60");
    assert.equal(view.nextAfter, 43);
    assert.equal(view.state, "crashed");
    assert.equal(view.lastExit?.code, 1);
  });

  it("appends the lines of the same instance and skips any it already has", () => {
    const first = mergeLogPage(
      EMPTY_LOG_VIEW,
      page({ lines: [line(1), line(2)], next_after: 2 }),
    );
    const second = mergeLogPage(
      first,
      page({ lines: [line(2), line(3)], next_after: 3 }),
    );
    assert.deepEqual(seqs(second), [1, 2, 3]);
    assert.equal(second.nextAfter, 3);
  });

  it("keeps the view as it is when a poll brings nothing new", () => {
    const first = mergeLogPage(
      EMPTY_LOG_VIEW,
      page({ lines: [line(1)], next_after: 1 }),
    );
    const second = mergeLogPage(first, page({ lines: [], next_after: 1 }));
    assert.deepEqual(seqs(second), [1]);
    assert.equal(second.entries, first.entries);
  });

  it("clears the view when the instance changes and keeps only the new page", () => {
    const first = mergeLogPage(
      EMPTY_LOG_VIEW,
      page({ lines: [line(1), line(2)], next_after: 2 }),
    );
    const restarted = mergeLogPage(
      first,
      page({ instance: "bbbb", lines: [line(3, "appmux")], next_after: 3 }),
    );
    assert.deepEqual(seqs(restarted), [3]);
    assert.equal(restarted.instance, "bbbb");
  });

  it("clears the view when appmux restarted and has not started the app again", () => {
    const first = mergeLogPage(
      EMPTY_LOG_VIEW,
      page({ lines: [line(1)], next_after: 1 }),
    );
    const view = mergeLogPage(
      first,
      page({ state: "never_started", instance: null, next_after: 0 }),
    );
    assert.deepEqual(seqs(view), []);
    assert.equal(view.state, "never_started");
    assert.equal(view.nextAfter, 0);
  });

  it("puts a marker before the page's lines when the server left some out", () => {
    const first = mergeLogPage(
      EMPTY_LOG_VIEW,
      page({ lines: [line(1)], next_after: 1 }),
    );
    const view = mergeLogPage(
      first,
      page({ lines: [line(9), line(10)], next_after: 10, dropped: 7 }),
    );
    assert.deepEqual(seqs(view), [1, "gap:7", 9, 10]);
  });

  it("gives every entry a key no other entry of the view has", () => {
    let view = mergeLogPage(
      EMPTY_LOG_VIEW,
      page({ lines: [line(5)], next_after: 5, dropped: 4 }),
    );
    view = mergeLogPage(
      view,
      page({ lines: [line(9)], next_after: 9, dropped: 3 }),
    );
    const keys = view.entries.map((entry) => entry.key);
    assert.equal(new Set(keys).size, keys.length);
  });

  it(`keeps only the newest ${String(LOG_ENTRIES_KEPT_MAX)} entries`, () => {
    const many = Array.from({ length: LOG_ENTRIES_KEPT_MAX + 5 }, (_, index) =>
      line(index + 1),
    );
    const view = mergeLogPage(
      EMPTY_LOG_VIEW,
      page({ lines: many, next_after: many.length }),
    );
    assert.equal(view.entries.length, LOG_ENTRIES_KEPT_MAX);
    const firstKept = view.entries[0];
    assert.ok(firstKept?.kind === "line");
    assert.equal(firstKept.seq, 6);
  });
});

describe("filterLogEntries", () => {
  const entries: LogEntry[] = mergeLogPage(
    EMPTY_LOG_VIEW,
    page({
      lines: [line(1, "stdout"), line(2, "stderr"), line(3, "appmux")],
      next_after: 3,
      dropped: 2,
    }),
  ).entries;

  it("keeps every entry for all streams", () => {
    assert.equal(filterLogEntries(entries, "all").length, 4);
  });

  it("keeps the lines of one stream and the markers, which belong to every stream", () => {
    const kinds = filterLogEntries(entries, "stderr").map((entry) =>
      entry.kind === "line" ? entry.stream : "gap",
    );
    assert.deepEqual(kinds, ["gap", "stderr"]);
  });
});

describe("gapText", () => {
  // The server dropped lines before the ones on screen; the page never had them.
  it("says how many earlier lines are not shown, in the singular for one", () => {
    assert.equal(gapText(1), "1 earlier line is not shown");
    assert.equal(gapText(8), "8 earlier lines are not shown");
    assert.equal(gapText(1200), "1,200 earlier lines are not shown");
  });
});

describe("logCopyText", () => {
  it("writes one line per entry with its time and stream, and the marker in words", () => {
    const view = mergeLogPage(
      EMPTY_LOG_VIEW,
      page({
        lines: [line(2, "stderr", "Traceback (most recent call last):")],
        next_after: 2,
        dropped: 1,
      }),
    );
    assert.equal(
      logCopyText(view.entries),
      "1 earlier line is not shown\n2026-10-30T09:14:03Z stderr Traceback (most recent call last):",
    );
  });
});

describe("logLineTime", () => {
  it("shows the UTC clock time of a line, to the second", () => {
    assert.equal(logLineTime("2026-10-30T09:14:03.250Z"), "09:14:03");
  });

  it("says unknown for a time that cannot be read", () => {
    assert.equal(logLineTime("yesterday"), "unknown");
  });
});

describe("describeLastExit", () => {
  const at = "2026-10-30T09:14:03Z";

  it("names the exit code and how long the process ran", () => {
    assert.equal(
      describeLastExit({ code: 1, signal: null, at, ran_for_s: 2.3 }),
      "Exited with code 1 after 2.3 s",
    );
  });

  it("names the signal when one ended the process", () => {
    assert.equal(
      describeLastExit({ code: null, signal: "SIGKILL", at, ran_for_s: 0.4 }),
      "Stopped by signal SIGKILL after 400 ms",
    );
  });

  it("says only how long it ran when neither is known", () => {
    assert.equal(
      describeLastExit({ code: null, signal: null, at, ran_for_s: 75 }),
      "Ended after 1 min 15 s",
    );
  });
});

describe("isScrolledToBottom", () => {
  it("is true at the bottom and within a few pixels of it", () => {
    assert.equal(
      isScrolledToBottom({
        scrollTop: 600,
        scrollHeight: 1000,
        clientHeight: 400,
      }),
      true,
    );
    assert.equal(
      isScrolledToBottom({
        scrollTop: 596,
        scrollHeight: 1000,
        clientHeight: 400,
      }),
      true,
    );
  });

  it("is false once the person scrolled up", () => {
    assert.equal(
      isScrolledToBottom({
        scrollTop: 300,
        scrollHeight: 1000,
        clientHeight: 400,
      }),
      false,
    );
  });
});
