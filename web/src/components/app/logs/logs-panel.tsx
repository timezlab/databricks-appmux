"use client";

import { ArrowDownToLineIcon, PauseIcon, PlayIcon } from "lucide-react";
import { useId, useRef, useState } from "react";

import { Button } from "@/components/button";
import { CopyButton } from "@/components/copy-button";
import { EmptyState } from "@/components/empty-state";
import { ErrorNotice } from "@/components/error-notice";
import { LiveRegion } from "@/components/live-region";
import { Notice } from "@/components/notice";
import { RelativeTime } from "@/components/relative-time";
import { Section } from "@/components/section";
import { SkeletonLines } from "@/components/skeletons";
import { StatusBadge } from "@/components/status-badge";
import { Toggle } from "@/components/ui/toggle";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import {
  LOG_STREAM_FILTERS,
  describeLastExit,
  filterLogEntries,
  logCopyText,
} from "@/lib/app/logs";
import type { LogStreamFilter, LogView } from "@/lib/app/logs";
import { pollData } from "@/lib/poll/data";
import { appStatePresentation } from "@/lib/status-presentation";
import { AppSectionLink } from "../app-section-link";
import { LogLines } from "./log-lines";
import { useAppLogs } from "./use-app-logs";

const STREAM_FILTER_LABELS: Record<LogStreamFilter, string> = {
  all: "All",
  stdout: "stdout",
  stderr: "stderr",
  appmux: "appmux",
};

function isStreamFilter(value: string): value is LogStreamFilter {
  return (LOG_STREAM_FILTERS as readonly string[]).includes(value);
}

export interface LogsPanelProps {
  slug: string;
  appName: string;
}

/**
 * The Logs section: the last lines of the app's output, polled every 2 s while
 * the tab is visible (nothing while it is hidden; the next poll catches up),
 * with Pause, Wrap, a stream filter, Follow and Copy. The lines come from the
 * server's ring (2000 lines across restarts); a new start of the app clears
 * the view, as the contract asks.
 */
export function LogsPanel({ slug, appName }: LogsPanelProps) {
  const [isPaused, setIsPaused] = useState(false);
  const [isWrapped, setIsWrapped] = useState(true);
  const [isFollowing, setIsFollowing] = useState(true);
  const [filter, setFilter] = useState<LogStreamFilter>("all");
  const [announcement, setAnnouncement] = useState<string | null>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const boxId = useId();

  const poll = useAppLogs(slug, isPaused);
  const view = pollData(poll);

  // A new start of the app clears the view; say so, or the lines just vanish.
  const [seenInstance, setSeenInstance] = useState<string | null>(null);
  const instance = view?.instance ?? null;
  if (instance !== seenInstance) {
    setSeenInstance(instance);
    if (seenInstance !== null && instance !== null) {
      setAnnouncement("The app started again; the view shows the new start.");
    }
  }

  function togglePause(): void {
    const next = !isPaused;
    setIsPaused(next);
    setAnnouncement(
      next
        ? "Paused: no new lines are fetched."
        : "Resumed: new lines show again.",
    );
  }

  function jumpToLatest(): void {
    setIsFollowing(true);
    boxRef.current?.focus();
  }

  if (view === null) {
    if (poll.status === "failed") {
      return (
        <Section title="Logs">
          <ErrorNotice
            error={poll.error}
            context="Could not load the logs"
            onRetry={poll.refresh}
          />
        </Section>
      );
    }
    return (
      <Section title="Logs">
        <p role="status" className="sr-only">
          Loading the logs
        </p>
        <div className="rounded-control border bg-card p-3">
          <SkeletonLines lines={8} />
        </div>
      </Section>
    );
  }

  if (view.state === "never_started") {
    return (
      <Section title="Logs">
        <EmptyState
          title="No output yet"
          action={
            <AppSectionLink
              slug={slug}
              section="overview"
              buttonVariant="secondary"
            >
              Go to Overview
            </AppSectionLink>
          }
        >
          appmux has not started this app since it started itself. Start it from
          Overview; its output shows here within 2 seconds.
        </EmptyState>
      </Section>
    );
  }

  const visible = filterLogEntries(view.entries, filter);
  const label = `Output of ${appName}`;

  return (
    <Section
      title="Logs"
      actions={
        <span className="text-sm text-muted-foreground">
          {isPaused ? "Paused" : "Updates every 2 seconds"}
        </span>
      }
    >
      <LogFacts view={view} />
      {poll.status === "stale" ? (
        <ErrorNotice
          error={poll.error}
          context="Could not refresh the logs"
          onRetry={poll.refresh}
        />
      ) : null}
      <div
        className="flex flex-wrap items-center gap-2"
        role="group"
        aria-label="Log view"
      >
        <Button size="small" onClick={togglePause}>
          {isPaused ? (
            <PlayIcon aria-hidden="true" />
          ) : (
            <PauseIcon aria-hidden="true" />
          )}
          {isPaused ? "Resume" : "Pause"}
        </Button>
        <ToggleGroup
          type="single"
          variant="outline"
          size="sm"
          aria-label="Stream"
          value={filter}
          onValueChange={(value) => {
            // A single group sends "" when its pressed item is pressed again; keep the choice.
            if (isStreamFilter(value)) setFilter(value);
          }}
        >
          {LOG_STREAM_FILTERS.map((stream) => (
            <ToggleGroupItem key={stream} value={stream}>
              {STREAM_FILTER_LABELS[stream]}
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
        <Toggle
          variant="outline"
          size="sm"
          pressed={isWrapped}
          onPressedChange={setIsWrapped}
        >
          Wrap
        </Toggle>
        <Toggle
          variant="outline"
          size="sm"
          pressed={isFollowing}
          onPressedChange={setIsFollowing}
        >
          Follow
        </Toggle>
        <CopyButton
          value={logCopyText(visible)}
          label="the shown lines"
          selectTargetId={boxId}
        />
      </div>
      {view.entries.length === 0 ? (
        <EmptyState title="Nothing printed yet">
          The app has written nothing to stdout or stderr since it started. New
          lines show here within 2 seconds.
        </EmptyState>
      ) : (
        <div className="relative flex flex-col gap-2">
          <LogLines
            id={boxId}
            boxRef={boxRef}
            label={label}
            entries={visible}
            isWrapped={isWrapped}
            isFollowing={isFollowing}
            onScrolledUp={() => {
              setIsFollowing(false);
            }}
          />
          {visible.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No {STREAM_FILTER_LABELS[filter]} lines in the view. Choose All to
              see every line.
            </p>
          ) : null}
          {isFollowing ? null : (
            <div className="flex justify-end">
              <Button size="small" onClick={jumpToLatest}>
                <ArrowDownToLineIcon aria-hidden="true" />
                Jump to latest
              </Button>
            </div>
          )}
        </div>
      )}
      <LiveRegion message={announcement} />
    </Section>
  );
}

/** The app's state and, when it is not running, how its last process ended. */
function LogFacts({ view }: { view: LogView }) {
  const state = view.state;
  if (state === null || state === "never_started") return null;
  const lastExit = state === "running" ? null : view.lastExit;
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
        <StatusBadge presentation={appStatePresentation(state)} />
        {lastExit === null ? null : (
          <span className="text-muted-foreground">
            Last exit: {describeLastExit(lastExit)},{" "}
            <RelativeTime value={lastExit.at} />
          </span>
        )}
      </div>
      {state === "crashed" ? (
        <Notice tone="danger" title="The app crashed">
          appmux stopped restarting it after repeated crashes. The lines below
          are from its last start; restart it from Overview once the cause is
          fixed.
        </Notice>
      ) : null}
    </div>
  );
}
