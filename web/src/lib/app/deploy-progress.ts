/**
 * Where a Deploy stands, derived from the polled `AppDetail` alone, so a page
 * that was just reloaded shows it as well as the page that pressed the button.
 * A release walks pending -> building -> starting -> healthy -> active
 * (design 5.1); a failed one is reported by Overview, not here.
 */
import type { AppDetail, ReleaseStatus } from "@/lib/api";
import { releaseStatusPresentation } from "@/lib/status-presentation";
import { activeRelease, inProgressRelease } from "./status";

const DEPLOY_STEPS: readonly ReleaseStatus[] = [
  "pending",
  "building",
  "starting",
  "healthy",
  "active",
];

export type DeployStepState = "done" | "current" | "todo";

export interface DeployStep {
  label: string;
  state: DeployStepState;
}

export interface DeployProgress {
  releaseNo: number;
  /** The word for the step the release is on ("Building"). */
  currentLabel: string;
  steps: DeployStep[];
  /** The release answering requests meanwhile; null when none does. */
  servingReleaseNo: number | null;
  /** One sentence on what the route does until this release is healthy. */
  outlook: string;
  /**
   * Whether the log already holds this release's output. The build is not
   * recorded; the new copy's lines arrive from `starting` on. Before that the
   * log shows the copy that is running.
   */
  isBuildOutputRecorded: boolean;
}

/** The Deploy under way, or null when no release is being built or started. */
export function deployProgress(app: AppDetail): DeployProgress | null {
  const release = inProgressRelease(app);
  if (release === null) return null;

  const at = DEPLOY_STEPS.indexOf(release.status);
  const active = activeRelease(app);
  // A stopped or restarting app serves nobody, whatever its release says.
  const servingReleaseNo =
    active !== null && app.state === "running" ? active.release_no : null;

  return {
    releaseNo: release.release_no,
    currentLabel: releaseStatusPresentation(release.status).label,
    steps: DEPLOY_STEPS.map((status, index) => ({
      label: releaseStatusPresentation(status).label,
      state: index < at ? "done" : index === at ? "current" : "todo",
    })),
    servingReleaseNo,
    outlook:
      servingReleaseNo === null
        ? "The route opens once its health check passes."
        : `Release ${servingReleaseNo} keeps serving until this one is healthy.`,
    isBuildOutputRecorded: at >= DEPLOY_STEPS.indexOf("starting"),
  };
}
