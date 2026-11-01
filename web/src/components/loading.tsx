import { LoaderCircleIcon } from "lucide-react";

export interface LoadingProps {
  /** What is loading, in words ("Loading apps"). Default "Loading". */
  label?: string;
}

/** A spinner with words, announced politely; the spinner alone would say nothing. */
export function Loading({ label = "Loading" }: LoadingProps) {
  return (
    <div
      className="flex items-center gap-2 py-4 text-muted-foreground"
      role="status"
    >
      <LoaderCircleIcon
        className="size-4 shrink-0 animate-spin motion-reduce:animate-none"
        aria-hidden="true"
      />
      <span>{label}…</span>
    </div>
  );
}
