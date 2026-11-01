/**
 * How the closed lists of the contract (admin-api.md: release `status`, app
 * `state`, request `status`) read on screen. Each value has a word, a mark and
 * a tone; the tone becomes a colour, never the only signal. The records are
 * typed over the schema enums, so a value added to the contract does not compile
 * until it has a presentation here.
 */
import type { AppState, ReleaseStatus, RequestStatus } from "@/lib/api";

export type Tone = "neutral" | "info" | "success" | "warning" | "danger";

export interface StatusPresentation {
  /** The word shown. */
  label: string;
  tone: Tone;
  /** A short symbol drawn beside the word (hidden from screen readers; the word says it). One mark per tone. */
  mark: string;
  /** One sentence for the title of the badge. */
  description: string;
}

const MARK: Record<Tone, string> = {
  neutral: "○",
  info: "◐",
  success: "●",
  warning: "▲",
  danger: "✕",
};

/**
 * The mark drawn beside a word of this tone. One per tone, so a person who
 * cannot see the colour still gets a different shape. For a page that has a
 * tone of its own (a check result, a memory level) instead of a status of the
 * contract; draw it in an `aria-hidden` span next to the word, as `StatusBadge` does.
 */
export function toneMark(tone: Tone): string {
  return MARK[tone];
}

function presentation(
  label: string,
  tone: Tone,
  description: string,
): StatusPresentation {
  return { label, tone, mark: toneMark(tone), description };
}

// Design 5.1: pending -> building -> starting -> healthy -> active, plus failed and superseded.
const RELEASE_STATUS: Record<ReleaseStatus, StatusPresentation> = {
  pending: presentation(
    "Pending",
    "neutral",
    "Created and waiting to be built.",
  ),
  building: presentation(
    "Building",
    "info",
    "Packages are being resolved and the release prepared.",
  ),
  starting: presentation(
    "Starting",
    "info",
    "The process started; waiting for the first healthy answer.",
  ),
  healthy: presentation(
    "Healthy",
    "success",
    "Passed the health check; it becomes active next.",
  ),
  active: presentation(
    "Active",
    "success",
    "This release is serving requests.",
  ),
  failed: presentation(
    "Failed",
    "danger",
    "Did not become healthy; it can still be activated by hand.",
  ),
  superseded: presentation(
    "Superseded",
    "neutral",
    "Replaced by a newer active release; it can be activated again.",
  ),
};

const APP_STATE: Record<AppState, StatusPresentation> = {
  stopped: presentation(
    "Stopped",
    "neutral",
    "Not running; its route is closed.",
  ),
  starting: presentation(
    "Starting",
    "info",
    "The process is starting; the route opens once the health check passes.",
  ),
  running: presentation(
    "Running",
    "success",
    "Running and answering requests.",
  ),
  crashed: presentation(
    "Crashed",
    "danger",
    "Stopped after repeated crashes; its route answers 503 until an admin acts.",
  ),
};

const REQUEST_STATUS: Record<RequestStatus, StatusPresentation> = {
  pending: presentation(
    "Pending",
    "warning",
    "Waiting for a platform admin's decision.",
  ),
  approved: presentation(
    "Approved",
    "success",
    "A platform admin approved it and the change was made.",
  ),
  denied: presentation(
    "Denied",
    "danger",
    "A platform admin denied it, or it closed automatically.",
  ),
  withdrawn: presentation(
    "Withdrawn",
    "neutral",
    "The person who asked took it back.",
  ),
};

export function releaseStatusPresentation(
  status: ReleaseStatus,
): StatusPresentation {
  return RELEASE_STATUS[status];
}

export function appStatePresentation(state: AppState): StatusPresentation {
  return APP_STATE[state];
}

export function requestStatusPresentation(
  status: RequestStatus,
): StatusPresentation {
  return REQUEST_STATUS[status];
}
