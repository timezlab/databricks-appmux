/**
 * The three views of `GET /requests` (design 9.4, admin-api.md `RequestList`).
 * The server sends one set, oldest first; the client builds the views, so every
 * page orders them the same way.
 */
import type { RequestRow } from "@/lib/api/schemas";

/** The platform admin's inbox: the pending requests, oldest first (the order sent). */
export function inboxRequests(items: readonly RequestRow[]): RequestRow[] {
  return items.filter((request) => request.status === "pending");
}

/** The launcher's own requests: those `actorEmail` asked for, newest first. */
export function ownRequests(
  items: readonly RequestRow[],
  actorEmail: string,
): RequestRow[] {
  return items
    .filter((request) => request.requested_by === actorEmail)
    .toReversed();
}

/** An app page's requests: those naming `appSlug`, newest first. */
export function appRequests(
  items: readonly RequestRow[],
  appSlug: string,
): RequestRow[] {
  return items.filter((request) => request.app_slug === appSlug).toReversed();
}
