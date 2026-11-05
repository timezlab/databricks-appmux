/**
 * The Logs section's view of an app's output (`GET /apps/{app}/logs`), kept as
 * pure functions so the merge rules test without a browser.
 *
 * The server keeps one ring per app (2000 lines or 256 KiB) whose `seq` runs on
 * across the app's restarts; every start has a new `instance`. The page polls
 * with `after` set to the last answer's `next_after` and merges each answer into
 * what it holds: lines of the same instance are appended (never twice), a new
 * instance clears the view first, and lines the server left out (`dropped`)
 * become one marker line, so a gap is never silent.
 *
 * The view keeps its entries oldest first, the order they arrive in; the page
 * shows them newest first (`newestFirst`), so the line a person wants is at the
 * top without scrolling.
 */
import type { AppLogs } from "@/lib/api";
import {
  UNKNOWN_TEXT,
  formatDurationMs,
  parseTime,
  pluralize,
} from "@/lib/format";

/** What the page keeps in memory: the server's ring holds at most this many lines too. */
export const LOG_ENTRIES_KEPT_MAX = 2000;

/** How often the page asks for new lines while the tab is visible and the view is not paused. */
export const LOG_POLL_INTERVAL_MS = 2000;

/** How close to the top (px) still counts as "at the latest line" for Follow: the newest line is shown first. */
const TOP_SLACK_PX = 8;

const MS_PER_SECOND = 1000;

type LogLine = AppLogs["lines"][number];
export type LogStream = LogLine["stream"];
export type LogStreamFilter = "all" | LogStream;

export const LOG_STREAM_FILTERS: readonly LogStreamFilter[] = [
  "all",
  "stdout",
  "stderr",
  "appmux",
];

export type LogEntry =
  | ({ kind: "line"; key: string } & LogLine)
  /** `count` lines the server no longer had, or that its page size left out. */
  | { kind: "gap"; key: string; count: number };

export interface LogView {
  /** The start the lines belong to; `null` before the first answer and when appmux has started no copy. */
  instance: string | null;
  /** Oldest first, at most `LOG_ENTRIES_KEPT_MAX`. */
  entries: LogEntry[];
  /** The `after` of the next poll. */
  nextAfter: number;
  state: AppLogs["state"] | null;
  lastExit: AppLogs["last_exit"];
}

export const EMPTY_LOG_VIEW: LogView = {
  instance: null,
  entries: [],
  nextAfter: 0,
  state: null,
  lastExit: null,
};

function gapEntry(afterSeq: number, count: number): LogEntry {
  return { kind: "gap", key: `gap-after-${String(afterSeq)}`, count };
}

function lineEntry(line: LogLine): LogEntry {
  return { kind: "line", key: `line-${String(line.seq)}`, ...line };
}

/**
 * `view` with one answer of the server folded in. The same instance appends
 * the lines after the cursor; a different instance (a restart of the app, or of
 * appmux itself) starts the view again from this page.
 */
export function mergeLogPage(view: LogView, page: AppLogs): LogView {
  const isSameStart =
    view.state !== null &&
    page.instance !== null &&
    page.instance === view.instance;
  const kept = isSameStart ? view.entries : [];
  const cursor = isSameStart ? view.nextAfter : 0;

  const fresh = page.lines.filter((line) => line.seq > cursor);
  const added: LogEntry[] = [];
  if (page.dropped > 0) added.push(gapEntry(cursor, page.dropped));
  for (const line of fresh) added.push(lineEntry(line));

  const entries =
    added.length === 0
      ? kept
      : [...kept, ...added].slice(-LOG_ENTRIES_KEPT_MAX);
  return {
    instance: page.instance,
    entries,
    nextAfter: page.next_after,
    state: page.state,
    lastExit: page.last_exit,
  };
}

/**
 * The entries one stream shows whose text holds `query` (any case, blank space
 * around it ignored, read as plain text and never as a pattern). A gap marker
 * is about every stream and every search, so it stays.
 */
export function filterLogEntries(
  entries: readonly LogEntry[],
  filter: LogStreamFilter,
  query = "",
): LogEntry[] {
  const needle = query.trim().toLowerCase();
  return entries.filter(
    (entry) =>
      entry.kind === "gap" ||
      ((filter === "all" || entry.stream === filter) &&
        (needle === "" || entry.text.toLowerCase().includes(needle))),
  );
}

/** How many of `entries` are lines of output; the gap markers are not. */
export function countLogLines(entries: readonly LogEntry[]): number {
  return entries.filter((entry) => entry.kind === "line").length;
}

/** The entries as the page shows them: the latest line first, the marker for the lines before them last. */
export function newestFirst(entries: readonly LogEntry[]): LogEntry[] {
  return entries.toReversed();
}

/**
 * "3 earlier lines are not shown": the server dropped lines before the ones on
 * screen, so the page never had them to show.
 */
export function gapText(count: number): string {
  return `${pluralize(count, "earlier line")} ${count === 1 ? "is" : "are"} not shown`;
}

/** What Copy puts on the clipboard: the shown entries, one per line, with the full time. */
export function logCopyText(entries: readonly LogEntry[]): string {
  return entries
    .map((entry) =>
      entry.kind === "gap"
        ? gapText(entry.count)
        : `${entry.at} ${entry.stream} ${entry.text}`,
    )
    .join("\n");
}

/** `09:14:03`: the UTC clock time of a line (the full time is its hover title). */
export function logLineTime(at: string): string {
  const ms = parseTime(at);
  if (ms === null) return UNKNOWN_TEXT;
  return new Date(ms)
    .toISOString()
    .slice("2026-10-30T".length, "2026-10-30T09:14:03".length);
}

/** "Exited with code 1 after 2.3 s", in the words appmux writes into the output. */
export function describeLastExit(
  lastExit: NonNullable<AppLogs["last_exit"]>,
): string {
  const ranFor = formatDurationMs(lastExit.ran_for_s * MS_PER_SECOND);
  if (lastExit.signal !== null) {
    return `Stopped by signal ${lastExit.signal} after ${ranFor}`;
  }
  if (lastExit.code === null) return `Ended after ${ranFor}`;
  return `Exited with code ${String(lastExit.code)} after ${ranFor}`;
}

/** True when the scroll box shows its first line, the latest one, give or take a few pixels. */
export function isScrolledToTop(box: { scrollTop: number }): boolean {
  return box.scrollTop <= TOP_SLACK_PX;
}
