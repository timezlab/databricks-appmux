/**
 * Where a Deploy stands, derived from the polled `AppDetail` alone, so a page
 * that was just reloaded shows it as well as the page that pressed the button.
 * Before its release exists a Deploy reads the source folder
 * (`deploy_preparing`, which can take minutes); then the release walks
 * pending -> building -> starting -> healthy -> active (design 5.1). A failed
 * one is reported by Overview, not here.
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

const PREPARING_LABEL = "Reading the folder";

export type DeployStepState = "done" | "current" | "todo";

export interface DeployStep {
  label: string;
  state: DeployStepState;
}

export interface DeployProgress {
  /** The release being built; null while the Deploy still reads the folder. */
  releaseNo: number | null;
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
  if (release === null && app.deploy_preparing === null) return null;

  // Step 0 is the folder read; the release's own steps follow it.
  const at = release === null ? 0 : DEPLOY_STEPS.indexOf(release.status) + 1;
  const labels = [
    PREPARING_LABEL,
    ...DEPLOY_STEPS.map((status) => releaseStatusPresentation(status).label),
  ];
  const active = activeRelease(app);
  // A stopped or restarting app serves nobody, whatever its release says.
  const servingReleaseNo =
    active !== null && app.state === "running" ? active.release_no : null;

  return {
    releaseNo: release === null ? null : release.release_no,
    currentLabel: labels[at] ?? PREPARING_LABEL,
    steps: labels.map((label, index) => ({
      label,
      state: index < at ? "done" : index === at ? "current" : "todo",
    })),
    servingReleaseNo,
    outlook:
      servingReleaseNo === null
        ? "The route opens once its health check passes."
        : `Release ${servingReleaseNo} keeps serving until this one is healthy.`,
    isBuildOutputRecorded: at >= DEPLOY_STEPS.indexOf("starting") + 1,
  };
}
