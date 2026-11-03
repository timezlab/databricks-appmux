import type { AppState, ReleaseStatus, RequestStatus } from "@/lib/api";
import {
  appStatePresentation,
  releaseStatusPresentation,
  requestStatusPresentation,
} from "@/lib/status-presentation";
import type { StatusPresentation } from "@/lib/status-presentation";
import { Lamp, lampStateOfTone } from "./lamp";

/**
 * A status of the contract as a lamp and its word (`Lamp`). The word carries
 * the meaning; the lamp's colour and shape back it up, so nothing depends on
 * seeing colour. The sentence about it is the hover title. Kept as a thin
 * wrapper so the pages name the status, not how it is drawn.
 */
export function StatusBadge({
  presentation,
}: {
  presentation: StatusPresentation;
}) {
  return (
    <Lamp
      state={lampStateOfTone(presentation.tone)}
      label={presentation.label}
      description={presentation.description}
    />
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
