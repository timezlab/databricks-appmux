"use client";

import type { ReactNode } from "react";

import { Button } from "./button";
import { describeError } from "./error-message";
import type { ErrorMessage } from "./error-message";
import { Notice } from "./notice";

export interface ErrorNoticeProps {
  /** Whatever a call threw; `ApiError` gets its own wording per status (see `describeError`). */
  error: unknown;
  /** What was being done ("Could not save the app"); shown before the heading. */
  context?: string;
  /** Fetches the page's data again (`usePoll(...).refresh`). Offered after a 409, so the person sees the current state. */
  onReload?: () => void;
  /** Repeats the call that failed. Offered after a network, 503 or 5xx error; falls back to `onReload`. */
  onRetry?: () => void;
}

function reloadPage(): void {
  window.location.reload();
}

function actionButton(
  message: ErrorMessage,
  onReload: (() => void) | undefined,
  onRetry: (() => void) | undefined,
): ReactNode {
  switch (message.action) {
    case "reload-data":
      // Without a reload callback the page itself is the only way to fresh data.
      return (
        <Button size="small" onClick={onReload ?? reloadPage}>
          {onReload === undefined ? "Reload page" : "Reload"}
        </Button>
      );
    case "retry": {
      const handler = onRetry ?? onReload;
      return handler === undefined ? null : (
        <Button size="small" onClick={handler}>
          Try again
        </Button>
      );
    }
    case "reload-page":
      return (
        <Button size="small" onClick={reloadPage}>
          Reload page
        </Button>
      );
    case "none":
      return null;
  }
}

/**
 * An error as a notice that a screen reader announces at once. The server's
 * message is shown as text, never as markup. A 409 offers a reload; 401, 403,
 * 503 and the rest each read differently.
 */
export function ErrorNotice({
  error,
  context,
  onReload,
  onRetry,
}: ErrorNoticeProps) {
  const message = describeError(error);
  const title =
    context === undefined ? message.heading : `${context}: ${message.heading}`;
  const action = actionButton(message, onReload, onRetry);

  return (
    <Notice tone="danger" title={title} live="assertive">
      {message.detail === null ? null : <p>{message.detail}</p>}
      {message.hint === null ? null : <p>{message.hint}</p>}
      {action === null ? null : (
        <div className="flex flex-wrap items-center gap-2">{action}</div>
      )}
    </Notice>
  );
}
