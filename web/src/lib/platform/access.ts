/**
 * Whether the signed-in person may see the platform page, from `GET /me`
 * (`is_platform_admin`). The server checks the same on every platform route;
 * this only decides between the page and the no-access state, so a person who
 * is not an admin sees an explanation instead of a list of forbidden errors.
 *
 * Fail closed: the page opens only on a positive answer. While `GET /me` is
 * pending, or when it never answered, no platform data is requested.
 */
import type { MeResponse } from "@/lib/api/schemas";
import type { PollResult } from "@/lib/poll/schedule";

export type PlatformAccess =
  | { state: "checking" }
  | { state: "error"; error: unknown }
  | { state: "denied"; email: string }
  | { state: "allowed"; email: string };

export function platformAccess(me: PollResult<MeResponse>): PlatformAccess {
  switch (me.status) {
    case "loading":
      return { state: "checking" };
    case "failed":
      return { state: "error", error: me.error };
    // A stale result is the last good answer; a failed re-poll does not take access away.
    case "ready":
    case "stale":
      return me.data.is_platform_admin
        ? { state: "allowed", email: me.data.email }
        : { state: "denied", email: me.data.email };
  }
}
