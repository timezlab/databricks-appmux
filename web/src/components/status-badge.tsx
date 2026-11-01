import type { AppState, ReleaseStatus, RequestStatus } from "@/lib/api";
import {
  appStatePresentation,
  releaseStatusPresentation,
  requestStatusPresentation,
} from "@/lib/status-presentation";
import type { StatusPresentation } from "@/lib/status-presentation";
import { Badge } from "./ui/badge";

/**
 * A status as a word with a mark and a colour. The word carries the meaning;
 * the mark (one per tone) and the colour back it up, so nothing depends on
 * seeing colour. The sentence about it is the hover title.
 */
export function StatusBadge({
  presentation,
}: {
  presentation: StatusPresentation;
}) {
  return (
    <Badge variant={presentation.tone} title={presentation.description}>
      <span aria-hidden="true">{presentation.mark}</span>
      {presentation.label}
    </Badge>
  );
}

export function ReleaseStatusBadge({ status }: { status: ReleaseStatus }) {
  return <StatusBadge presentation={releaseStatusPresentation(status)} />;
}

export function AppStateBadge({ state }: { state: AppState }) {
  return <StatusBadge presentation={appStatePresentation(state)} />;
}

export function RequestStatusBadge({ status }: { status: RequestStatus }) {
  return <StatusBadge presentation={requestStatusPresentation(status)} />;
}
