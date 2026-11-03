"use client";

import { cn } from "cn";
import { useLayoutEffect } from "react";
import type { RefObject } from "react";

import type { LogEntry, LogStream } from "@/lib/app/logs";
import { gapText, isScrolledToBottom, logLineTime } from "@/lib/app/logs";
import { formatAbsoluteTime } from "@/lib/format";

/**
 * The stream column carries its name in words; the colour only backs it up.
 * stderr is a pipe, not a severity: Python's logging and uvicorn write their
 * INFO lines to it (uvicorn's access log goes to stdout), so a danger tone would
 * mark every start-up line of a healthy app as an error. It is only a step
 * brighter than stdout.
 */
const STREAM_CLASSES: Record<LogStream, string> = {
  stdout: "text-muted-foreground",
  stderr: "text-foreground",
  appmux: "text-tone-info-text",
};

export interface LogLinesProps {
  id: string;
  /** "Output of Sales dashboard": the name of the scroll box. */
  label: string;
  entries: readonly LogEntry[];
  isWrapped: boolean;
  /** Keeps the box scrolled to its last line as lines arrive. */
  isFollowing: boolean;
  /** Called when the person scrolls away from the last line, so Follow turns off. */
  onScrolledUp: () => void;
  /** The scroll box, so the panel can move focus into it ("Jump to latest"). */
  boxRef: RefObject<HTMLDivElement | null>;
}

/**
 * The lines of an app's output in a scroll box: time, stream and text in mono,
 * each as a text node (React escapes it; nothing in a line is ever markup). The
 * box takes the keyboard (Tab, then the arrow keys and Page Down scroll it).
 */
export function LogLines({
  id,
  label,
  entries,
  isWrapped,
  isFollowing,
  onScrolledUp,
  boxRef,
}: LogLinesProps) {
  const lastKey = entries.at(-1)?.key ?? "";

  // Before paint, so a new line never flashes above the fold first.
  useLayoutEffect(() => {
    const box = boxRef.current;
    if (!isFollowing || box === null) return;
    box.scrollTop = box.scrollHeight;
  }, [boxRef, isFollowing, lastKey, entries.length, isWrapped]);

  return (
    <div
      ref={boxRef}
      id={id}
      role="log"
      // A poll every 2 s would read each new line aloud; the person reads the box when they choose.
      aria-live="off"
      aria-label={label}
      tabIndex={0}
      onScroll={(event) => {
        if (isFollowing && !isScrolledToBottom(event.currentTarget)) {
          onScrolledUp();
        }
      }}
      className="max-h-[min(28rem,70vh)] min-h-24 overflow-auto rounded-control border bg-card py-2 font-mono text-xs leading-5 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
      data-wrapped={isWrapped}
    >
      <ol
        className={cn(
          "flex flex-col",
          isWrapped ? "w-full" : "w-max min-w-full",
        )}
      >
        {entries.map((entry) =>
          entry.kind === "gap" ? (
            <li
              key={entry.key}
              className="mx-3 my-1 border-y border-dashed py-0.5 text-center text-muted-foreground italic"
              data-log-gap=""
            >
              {gapText(entry.count)}
            </li>
          ) : (
            <li
              key={entry.key}
              className="grid grid-cols-[auto_auto_minmax(0,1fr)] gap-x-3 px-3 hover:bg-muted"
              data-stream={entry.stream}
            >
              <time
                dateTime={entry.at}
                title={formatAbsoluteTime(entry.at, { seconds: true })}
                className="text-muted-foreground tabular-nums"
              >
                {logLineTime(entry.at)}
              </time>
              <span className={STREAM_CLASSES[entry.stream]}>
                {entry.stream}
              </span>
              <span
                className={
                  isWrapped
                    ? "whitespace-pre-wrap wrap-anywhere"
                    : "whitespace-pre"
                }
              >
                {entry.text}
              </span>
            </li>
          ),
        )}
      </ol>
    </div>
  );
}
