/**
 * Turns whatever a call threw into the words and the next step shown to the
 * person. `lib/api` turns every failure into an `ApiError` (HTTP status, a short
 * `code`, a message safe to show), so a page handles one type; anything else
 * is a bug in the page and reads as "Something went wrong".
 *
 * `detail` is the server's own message and is data: the renderer shows it as
 * text (React escapes it), never as markup.
 */
import {
  ApiError,
  INVALID_REQUEST,
  INVALID_RESPONSE,
  NETWORK_ERROR,
} from "@/lib/api";
import { RELOAD_CONFLICT_CODES } from "@/lib/app/actions";

export type ErrorKind =
  | "conflict"
  | "signed-out"
  | "forbidden"
  | "not-found"
  | "rejected"
  | "unavailable"
  | "server"
  | "network"
  | "unexpected-response"
  | "unknown";

/**
 * What the notice offers:
 * - `reload-data`: fetch the data again (the page's own reload, so unsaved input elsewhere survives);
 * - `reload-page`: reload the browser page (to sign in again, or to recover from a bug);
 * - `retry`: repeat the call that failed;
 * - `none`: nothing the person can do here.
 */
export type ErrorAction = "reload-data" | "reload-page" | "retry" | "none";

export interface ErrorMessage {
  kind: ErrorKind;
  heading: string;
  /** The server's message, shown as text; `null` when there is none worth showing. */
  detail: string | null;
  /** What to do next, one sentence; `null` when the heading says it all. */
  hint: string | null;
  action: ErrorAction;
}

const FIRST_SERVER_ERROR = 500;
const LAST_SERVER_ERROR = 599;
// admin-api.md, Conventions: a stale `version` answers 409 with this message.
const STALE_VERSION = /changed by someone else/i;

/**
 * A 409 offers a reload only when its code says the page was stale (the codes
 * after which the app page reads the app again). Any other 409 refuses the
 * write as it stands (a taken slug, a protected release, an Update that
 * already retried): fresh data would show the same refusal, so the server's
 * reason is the whole message.
 */
function conflict(error: ApiError): ErrorMessage {
  const isStale = RELOAD_CONFLICT_CODES.has(error.code);
  return {
    kind: "conflict",
    heading: STALE_VERSION.test(error.message)
      ? "Changed by someone else"
      : "Not possible in the current state",
    detail: error.message,
    hint: isStale ? "Reload to see the current state, then retry." : null,
    action: isStale ? "reload-data" : "none",
  };
}

function describeApiError(error: ApiError): ErrorMessage {
  const { status, code, message } = error;

  // The client's own refusal to send a call also has status 0, but no
  // connection was lost: it falls through to the generic notice below.
  if (code === NETWORK_ERROR || (status === 0 && code !== INVALID_REQUEST)) {
    return {
      kind: "network",
      heading: "Could not reach appmux",
      detail: null,
      // A lapsed Databricks session answers with a redirect to a login page,
      // which a same-origin fetch reports as a network failure, not a 401.
      hint: "Check the connection and try again. If you were signed out, reload the page to sign in again.",
      action: "retry",
    };
  }
  if (code === INVALID_RESPONSE) {
    return {
      kind: "unexpected-response",
      heading: "Unexpected answer from the server",
      detail: message,
      hint: null,
      action: "reload-page",
    };
  }

  if (status === 409) return conflict(error);
  if (status === 401) {
    return {
      kind: "signed-out",
      heading: "You are not signed in",
      detail: message,
      hint: "Reload the page to sign in again.",
      action: "reload-page",
    };
  }
  if (status === 403) {
    return {
      kind: "forbidden",
      heading: "You do not have permission",
      detail: message,
      hint: null,
      action: "none",
    };
  }
  if (status === 404) {
    return {
      kind: "not-found",
      heading: "Not found",
      detail: message,
      hint: "It may have been deleted or renamed.",
      action: "none",
    };
  }
  if (status === 400) {
    return {
      kind: "rejected",
      heading: "The change was not accepted",
      detail: message,
      hint: null,
      action: "none",
    };
  }
  if (status === 503) {
    return {
      kind: "unavailable",
      heading: "appmux is not available right now",
      detail: message,
      hint: "Try again in a moment.",
      action: "retry",
    };
  }
  if (status >= FIRST_SERVER_ERROR && status <= LAST_SERVER_ERROR) {
    return {
      kind: "server",
      heading: "The server hit an error",
      detail: message,
      hint: "Try again; tell a platform admin if it keeps happening.",
      action: "retry",
    };
  }
  return {
    kind: "unknown",
    heading: "The request failed",
    detail: message,
    hint: null,
    action: "none",
  };
}

export function describeError(error: unknown): ErrorMessage {
  if (error instanceof ApiError) return describeApiError(error);
  return {
    kind: "unknown",
    heading: "Something went wrong",
    // A thrown string or object may hold anything; only an Error's message is shown.
    detail: error instanceof Error ? error.message : null,
    hint: "Reload the page and try again.",
    action: "reload-page",
  };
}
