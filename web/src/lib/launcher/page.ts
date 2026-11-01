/**
 * Decisions about the launcher page as a whole: when it has nothing to offer,
 * how often each answer is polled, and what an empty list says.
 */
import { ApiError, HTTP_FORBIDDEN } from "@/lib/api";
import type { AppList } from "@/lib/api";
import type { RegisterFormContext } from "./register-form";
import { pendingRegisterSlugs } from "./requests";
import type { OwnRequestsSection } from "./requests";
import { pollData } from "@/lib/poll/data";
import type { PollResult } from "@/lib/poll/schedule";
import { ownRequests } from "@/lib/request-views";

/**
 * `GET /me` changes only when a platform-admin entry or a group does (the group
 * cache lives 300 s, design 9.1), so it is read far less often than the lists.
 */
export const ME_POLL_INTERVAL_MS = 30_000;

const HTTP_UNAUTHORIZED = 401;

/**
 * The error that leaves the page with nothing to offer: the very first answer
 * was "not signed in" or "not allowed", so every section would fail the same
 * way and the page shows that one notice. Once data has been shown, a later 401
 * leaves it on screen with the notice above it, so a form in progress is not
 * lost. `null` for everything else.
 */
export function accessBlockingError(
  apps: PollResult<AppList>,
): ApiError | null {
  if (apps.status !== "failed") return null;
  const { error } = apps;
  if (!(error instanceof ApiError)) return null;
  return error.status === HTTP_UNAUTHORIZED || error.status === HTTP_FORBIDDEN
    ? error
    : null;
}

// The launcher component reads it from here; it lives in lib/poll.
export { pollData };

/**
 * What the request form checks before it sends (the server decides anyway):
 * the slugs the visitor already sees, and their pending `register` slugs, which
 * stay unknown until their requests have loaded.
 */
export function registerFormContext(
  apps: PollResult<AppList>,
  requests: OwnRequestsSection,
): RegisterFormContext {
  const knownSlugs = (pollData(apps)?.items ?? []).map((app) => app.slug);
  if (requests.status !== "ready") {
    return { knownSlugs, pendingRegisterSlugs: null };
  }
  return {
    knownSlugs,
    pendingRegisterSlugs: pendingRegisterSlugs(
      ownRequests(requests.items, requests.email),
    ),
  };
}

export interface EmptyCopy {
  title: string;
  body: string;
}

/** What the apps section says when the list is empty; `isPlatformAdmin` is `null` while `GET /me` has not answered. */
export function appsEmptyCopy(isPlatformAdmin: boolean | null): EmptyCopy {
  if (isPlatformAdmin === true) {
    return {
      title: "No apps are registered yet",
      body: "Register the first one on the Platform page, or answer a request for a new app in its inbox.",
    };
  }
  return {
    title: "No apps for you yet",
    body: "Ask the owner of an app to share it with you, or request a new app.",
  };
}
