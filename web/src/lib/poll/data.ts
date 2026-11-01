import type { PollResult } from "./schedule";

/** The last good value of a poll: present while ready, and kept while a later call fails. */
export function pollData<T>(result: PollResult<T>): T | null {
  return result.status === "ready" || result.status === "stale"
    ? result.data
    : null;
}
