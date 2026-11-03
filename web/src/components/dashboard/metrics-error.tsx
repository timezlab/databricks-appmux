import { ErrorNotice } from "@/components/error-notice";
import { Notice } from "@/components/notice";
import { isWindowTooLong } from "@/lib/dashboard/scope";

export interface MetricsErrorProps {
  error: unknown;
  /** "Could not load the metrics"; shown before the heading. */
  context: string;
  /** Asks again (`usePoll(...).refresh`). */
  onRetry: () => void;
}

/**
 * A failed metrics call. A `400 window_too_long` gets its own words: the
 * generic 400 reads "The change was not accepted", which says nothing here, and
 * the ranges of this page never ask for more than the contract allows, so it
 * means the page and the server disagree on the limits.
 */
export function MetricsError({ error, context, onRetry }: MetricsErrorProps) {
  if (isWindowTooLong(error)) {
    return (
      <Notice
        tone="danger"
        title={`${context}: the range is longer than the server answers`}
        live="assertive"
      >
        <p>
          The server reads at most 7 days at a time, in at most 1,440 steps, and
          refused this range. The admin UI and the server may be of different
          versions. Pick a shorter range, or reload the page.
        </p>
      </Notice>
    );
  }
  return (
    <ErrorNotice
      error={error}
      context={context}
      onRetry={onRetry}
      onReload={onRetry}
    />
  );
}
