import { useRef } from "react";

import { usePoll } from "@/hooks/use-poll";
import type { UsePoll } from "@/hooks/use-poll";
import { getAppLogs } from "@/lib/api";
import {
  EMPTY_LOG_VIEW,
  LOG_POLL_INTERVAL_MS,
  mergeLogPage,
} from "@/lib/app/logs";
import type { LogView } from "@/lib/app/logs";

/** The most the route returns in one page (admin-api.md addendum): a first read gets as much of the ring as it can. */
const LOG_PAGE_LIMIT = 1000;

/**
 * The output of `slug`, polled every 2 s while the tab is visible and
 * `isPaused` is false. Each answer is merged into the view the last one left
 * (`mergeLogPage`), and the next call asks only for what came after it, so a
 * hidden tab or a pause costs nothing and the first poll after it catches up.
 * `usePoll` never runs two calls at once, so the view in the ref is always the
 * one the next answer belongs after.
 */
export function useAppLogs(slug: string, isPaused: boolean): UsePoll<LogView> {
  const viewRef = useRef<LogView>(EMPTY_LOG_VIEW);
  return usePoll(
    async (signal) => {
      const page = await getAppLogs(
        slug,
        { after: viewRef.current.nextAfter, limit: LOG_PAGE_LIMIT },
        { signal },
      );
      viewRef.current = mergeLogPage(viewRef.current, page);
      return viewRef.current;
    },
    {
      intervalMs: LOG_POLL_INTERVAL_MS,
      enabled: !isPaused,
      resetKey: `logs:${slug}`,
    },
  );
}
