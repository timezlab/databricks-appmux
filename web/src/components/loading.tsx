import { LoaderCircleIcon } from "lucide-react";

import { SkeletonLines } from "./skeletons";

export interface LoadingProps {
  /** What is loading, in words ("Loading apps"). Default "Loading". */
  label?: string;
}

/**
 * A spinner with words, announced politely (the spinner alone would say
 * nothing), over a few skeleton lines that hold the place of the content. The
 * spinner turns, or pulses when the person asked for less motion; it never
 * stands still.
 */
export function Loading({ label = "Loading" }: LoadingProps) {
  return (
    <div className="flex flex-col gap-3 py-4">
      <div
        className="flex items-center gap-2 text-sm text-muted-foreground"
        role="status"
      >
        <LoaderCircleIcon
          className="size-4 shrink-0 animate-spinner"
          aria-hidden="true"
        />
        <span>{label}…</span>
      </div>
      <SkeletonLines />
    </div>
  );
}
