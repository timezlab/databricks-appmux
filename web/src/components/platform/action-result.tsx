"use client";

import { ErrorNotice } from "@/components/error-notice";
import { Notice } from "@/components/notice";
import { usePlatformContext } from "./platform-context";
import type { WriteAction } from "./use-write-action";

/**
 * What a write came to: the error (a 409 offers a reload, a network error a
 * retry) or the sentence of the outcome. The outcome is also in the live region
 * of the page, so the notice itself does not announce again.
 */
export function ActionResult({
  action,
  context,
}: {
  action: Pick<WriteAction, "state" | "retry">;
  /** What was being done ("Could not save the app"). */
  context: string;
}) {
  const { refresh } = usePlatformContext();
  const { state } = action;
  switch (state.status) {
    case "idle":
    case "busy":
      return null;
    case "failed":
      return (
        <ErrorNotice
          error={state.error}
          context={context}
          onReload={refresh}
          onRetry={action.retry}
        />
      );
    case "done":
      return (
        <Notice tone={state.outcome.tone} title={state.outcome.title}>
          {state.outcome.detail === undefined ? null : (
            <p>{state.outcome.detail}</p>
          )}
        </Notice>
      );
  }
}
