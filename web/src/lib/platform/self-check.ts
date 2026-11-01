/**
 * The platform self-check as the page shows it (design 10.1): every check with
 * its own state, and one verdict on top.
 *
 * The verdict fails closed. "Passed" needs the overall flag and every listed
 * check; a result with no checks at all is "unverified", because a list that
 * proves nothing must not read as a pass on a page whose job is to say the
 * platform is safe to run apps on.
 */
import { toneMark } from "@/lib/status-presentation";
import type { StatusPresentation } from "@/lib/status-presentation";
import type { SelfCheckResult } from "@/lib/api/schemas";
import {
  formatCount,
  formatDurationMs,
  parseTime,
  pluralize,
} from "@/lib/format";

export type SelfCheckState = "passed" | "failed" | "unverified";

const PASSED: StatusPresentation = {
  label: "Passed",
  tone: "success",
  mark: toneMark("success"),
  description: "The check passed.",
};
const FAILED: StatusPresentation = {
  label: "Failed",
  tone: "danger",
  mark: toneMark("danger"),
  description: "The check failed.",
};
const UNVERIFIED: StatusPresentation = {
  label: "Unverified",
  tone: "warning",
  mark: toneMark("warning"),
  description: "The self-check returned no checks, so nothing was verified.",
};

export interface SelfCheckRow {
  name: string;
  state: "passed" | "failed";
  presentation: StatusPresentation;
  /** The check's own detail; empty when it sent none. */
  detail: string;
}

export interface SelfCheckView {
  state: SelfCheckState;
  presentation: StatusPresentation;
  /** One sentence: how many checks, how they went, how long they took. */
  summary: string;
  rows: SelfCheckRow[];
  failedNames: string[];
  ranAt: string;
}

export function selfCheckView(result: SelfCheckResult): SelfCheckView {
  const rows: SelfCheckRow[] = result.checks.map((check) => ({
    name: check.name,
    state: check.passed ? "passed" : "failed",
    presentation: check.passed ? PASSED : FAILED,
    detail: check.detail ?? "",
  }));
  const failedNames = rows
    .filter((row) => row.state === "failed")
    .map((row) => row.name);
  const took = formatDurationMs(result.duration_ms);
  const total = result.checks.length;

  let state: SelfCheckState;
  let presentation: StatusPresentation;
  let summary: string;
  if (failedNames.length > 0) {
    state = "failed";
    presentation = {
      ...FAILED,
      description: "At least one check failed; apps may not be safe to run.",
    };
    summary = `${formatCount(failedNames.length)} of ${pluralize(total, "check")} failed in ${took}`;
  } else if (!result.passed) {
    state = "failed";
    presentation = {
      ...FAILED,
      description: "The self-check reported a failure.",
    };
    summary = `The self-check reported a failure although no listed check failed (${pluralize(total, "check")}, ${took})`;
  } else if (total === 0) {
    state = "unverified";
    presentation = UNVERIFIED;
    summary = `The self-check ran no checks (${took}), so nothing was verified`;
  } else {
    state = "passed";
    presentation = {
      ...PASSED,
      description: "Every check passed.",
    };
    summary = `${pluralize(total, "check")} passed in ${took}`;
  }

  return {
    state,
    presentation,
    summary,
    rows,
    failedNames,
    ranAt: result.ran_at,
  };
}

/**
 * The result that ran later. The page holds the one `GET /platform` sent and,
 * after "Run again", the one `POST /platform/self-check` sent; a poll that was
 * already in flight must not replace the newer result with an older one.
 * A result whose time cannot be read loses to one whose time can; on a tie the
 * first argument wins.
 */
export function newerSelfCheck(
  first: SelfCheckResult | null,
  second: SelfCheckResult | null,
): SelfCheckResult | null {
  if (first === null) return second;
  if (second === null) return first;
  const firstMs = parseTime(first.ran_at);
  const secondMs = parseTime(second.ran_at);
  if (firstMs === null) return secondMs === null ? first : second;
  if (secondMs === null) return first;
  return secondMs > firstMs ? second : first;
}
