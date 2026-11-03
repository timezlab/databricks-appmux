import { usePoll } from "@/hooks/use-poll";
import type { UsePoll } from "@/hooks/use-poll";
import { getRelease } from "@/lib/api";
import type { ReleaseDetail } from "@/lib/api";

/** A failed release does not change by itself; a slow poll only catches a rename or a retry. */
const RELEASE_DETAIL_POLL_INTERVAL_MS = 60_000;

/**
 * One release's detail (`GET /apps/{app}/releases/{id}`): its output tail and
 * the failure hint, which the release list does not carry. Asked only while
 * `isEnabled`, so a list of failed releases costs one call per release the
 * person opens, not one per row.
 */
export function useReleaseDetail(
  slug: string,
  releaseNo: number | null,
  isEnabled: boolean,
): UsePoll<ReleaseDetail> {
  return usePoll(
    (signal) => {
      if (releaseNo === null) {
        return Promise.reject(new Error("No release to read."));
      }
      return getRelease(slug, releaseNo, { signal });
    },
    {
      intervalMs: RELEASE_DETAIL_POLL_INTERVAL_MS,
      enabled: isEnabled && releaseNo !== null,
      resetKey: `release:${slug}:${String(releaseNo)}`,
    },
  );
}
