"use client";

import { ChevronRightIcon } from "lucide-react";
import { useId, useState } from "react";

import { CopyButton } from "@/components/copy-button";
import { ErrorNotice } from "@/components/error-notice";
import { SkeletonLines } from "@/components/skeletons";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import type { ReleaseSummary } from "@/lib/api";
import { pollData } from "@/lib/poll/data";
import { FailureHint } from "./failure-hint";
import { useReleaseDetail } from "./use-release-detail";

export interface ReleaseFailureProps {
  slug: string;
  release: ReleaseSummary;
}

/**
 * Why a failed release failed, on its row: the reason, the server's hint when
 * it has one, and the last lines the release printed in a box that opens. The
 * failed target (the release the app was moving to) is read at once and opens
 * with its output shown, because that is the failure the person came for; an
 * older failed release is read when its output is opened.
 */
export function ReleaseFailure({ slug, release }: ReleaseFailureProps) {
  const isCurrentFailure = release.is_target;
  const [isOpen, setIsOpen] = useState(isCurrentFailure);
  const detail = useReleaseDetail(
    slug,
    release.release_no,
    isCurrentFailure || isOpen,
  );
  const data = pollData(detail);
  const outputId = useId();

  return (
    <div className="flex min-w-0 flex-col gap-2">
      <p className="wrap-break-word">
        {release.status_reason ?? "No reason was recorded."}
      </p>
      {data?.failure_hint == null ? null : (
        <FailureHint slug={slug} hint={data.failure_hint} />
      )}
      <Collapsible open={isOpen} onOpenChange={setIsOpen}>
        <CollapsibleTrigger className="group/output inline-flex items-center gap-1 rounded-sm text-sm font-medium text-link underline-offset-4 hover:underline focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">
          <ChevronRightIcon
            className="size-4 transition-transform group-data-[state=open]/output:rotate-90 motion-reduce:transition-none"
            aria-hidden="true"
          />
          {isOpen ? "Hide the output" : "Show the output"}
          <span className="sr-only"> of release {release.release_no}</span>
        </CollapsibleTrigger>
        <CollapsibleContent className="pt-2">
          {data === null ? (
            detail.status === "failed" ? (
              <ErrorNotice
                error={detail.error}
                context={`Could not load the output of release ${String(release.release_no)}`}
                onRetry={detail.refresh}
              />
            ) : (
              <>
                <p role="status" className="sr-only">
                  Loading the output
                </p>
                <SkeletonLines lines={4} />
              </>
            )
          ) : data.build_log_tail === null || data.build_log_tail === "" ? (
            <p className="text-sm text-muted-foreground">
              The release printed nothing before it stopped.
            </p>
          ) : (
            <div className="flex flex-col gap-1">
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm text-muted-foreground">
                  Last lines of its output
                </span>
                <CopyButton
                  value={data.build_log_tail}
                  label={`the output of release ${String(release.release_no)}`}
                  selectTargetId={outputId}
                />
              </div>
              <pre
                id={outputId}
                role="region"
                tabIndex={0}
                aria-label={`Output of release ${String(release.release_no)}`}
                className="max-h-64 overflow-auto rounded-control border bg-card p-2 font-mono text-xs leading-5 whitespace-pre-wrap wrap-anywhere"
              >
                {data.build_log_tail}
              </pre>
            </div>
          )}
        </CollapsibleContent>
      </Collapsible>
    </div>
  );
}
