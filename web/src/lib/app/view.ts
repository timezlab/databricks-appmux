/**
 * What the app page shows for the answer of `GET /apps/{app}`. That route
 * needs `CAN_MANAGE`, so a 403 is a visitor who may open the app but not
 * manage it, and a 404 is a slug that does not exist (or was deleted, or
 * renamed by this page, which then moves to the new slug).
 */
import { ApiError, HTTP_FORBIDDEN } from "@/lib/api";
import type { AppDetail } from "@/lib/api";
import { assertNever } from "@/lib/assert-never";
import type { PollResult } from "@/lib/poll/schedule";

const HTTP_NOT_FOUND = 404;

// Components read it from here; it lives in lib/poll.
export { pollData } from "@/lib/poll/data";

export type AppLoadView =
  | { kind: "loading" }
  | { kind: "not-found" }
  | { kind: "no-access" }
  | { kind: "failed"; error: unknown }
  | {
      kind: "ready";
      app: AppDetail;
      /** The latest refresh failed; the app on screen is the last good one. `null` when it did not. */
      refreshError: unknown;
    };

export interface AppLoadOptions {
  /**
   * This page renamed the app and has not moved to the new slug yet: it waits
   * for the owner change sent with the rename. The old slug answers 404 from
   * the rename on, so a 404 then keeps the copy on screen instead of saying
   * "App not found".
   */
  isMoving?: boolean;
}

function isNotFound(error: unknown): boolean {
  return error instanceof ApiError && error.status === HTTP_NOT_FOUND;
}

function refusalView(error: unknown): AppLoadView | null {
  if (!(error instanceof ApiError)) return null;
  if (isNotFound(error)) return { kind: "not-found" };
  if (error.status === HTTP_FORBIDDEN) return { kind: "no-access" };
  return null;
}

export function appLoadView(
  app: PollResult<AppDetail>,
  { isMoving = false }: AppLoadOptions = {},
): AppLoadView {
  switch (app.status) {
    case "loading":
      return { kind: "loading" };
    case "ready":
      return { kind: "ready", app: app.data, refreshError: null };
    case "stale":
      if (isMoving && isNotFound(app.error)) {
        return { kind: "ready", app: app.data, refreshError: null };
      }
      // A 404 or 403 after the app was shown is an answer, not a hiccup: the
      // app was deleted or access was withdrawn, so the old copy goes away.
      return (
        refusalView(app.error) ?? {
          kind: "ready",
          app: app.data,
          refreshError: app.error,
        }
      );
    case "failed":
      return refusalView(app.error) ?? { kind: "failed", error: app.error };
    default:
      return assertNever(app);
  }
}
