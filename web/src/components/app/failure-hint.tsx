import { LightbulbIcon } from "lucide-react";

import { AppSectionLink } from "./app-section-link";

export interface FailureHintProps {
  slug: string;
  /** `ReleaseDetail.failure_hint`: advice about the memory budget, in the server's words. */
  hint: string;
}

/**
 * The advice the server gives with a failed release, apart from its reason:
 * a light bulb and the word "Hint" mark it as a suggestion, not the error.
 * Every hint the contract names is about the memory budget, so the way to
 * change it sits beside the words.
 */
export function FailureHint({ slug, hint }: FailureHintProps) {
  return (
    <div
      className="flex items-start gap-2 rounded-control border border-tone-info-border bg-tone-info-bg px-3 py-2 text-sm text-tone-info-text"
      data-failure-hint=""
    >
      <LightbulbIcon className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <div className="flex min-w-0 flex-col gap-1">
        <p className="wrap-break-word">
          <span className="font-semibold">Hint: </span>
          {hint}
        </p>
        <p>
          <AppSectionLink slug={slug} section="settings">
            Change the memory budget
          </AppSectionLink>
        </p>
      </div>
    </div>
  );
}
