/**
 * What the app page says about the app as a whole, derived from the fields of
 * `AppDetail` and nothing else: which release serves, which one is the target
 * and what went wrong with it. The notices are plain data; the page draws them.
 */
import type { AppDetail, ReleaseSummary } from "@/lib/api";
import { isReleaseInProgress } from "./actions";
import type { AppSection } from "./params";
import { pluralize } from "@/lib/format";
import { releaseStatusPresentation } from "@/lib/status-presentation";
import type { Tone } from "@/lib/status-presentation";

// ---------------------------------------------------------------- releases

export function activeRelease(app: AppDetail): ReleaseSummary | null {
  return app.releases.find((release) => release.status === "active") ?? null;
}

/** The release being built or started on its way to active, if any. */
export function inProgressRelease(app: AppDetail): ReleaseSummary | null {
  return (
    app.releases.find((release) => isReleaseInProgress(release.status)) ?? null
  );
}

/** A target that failed: the app keeps the previous active release, or has none. */
export function failedTargetRelease(app: AppDetail): ReleaseSummary | null {
  return (
    app.releases.find(
      (release) => release.is_target && release.status === "failed",
    ) ?? null
  );
}

export function releasesNewestFirst(
  releases: readonly ReleaseSummary[],
): ReleaseSummary[] {
  return releases.toSorted((a, b) => b.release_no - a.release_no);
}

/** Small words beside a release's status. The active release is the target by definition, so it needs no mark. */
export function releaseMarkers(release: ReleaseSummary): string[] {
  return release.is_target && release.status !== "active" ? ["target"] : [];
}

/** The lock column: a hand lock names who set it; the active release is protected with no mark. */
export function releaseLockLabel(release: ReleaseSummary): string | null {
  if (release.locked_by !== null) return `Locked by ${release.locked_by}`;
  if (release.status === "active") return "Protected while active";
  return null;
}

/**
 * A poll can remove the release a panel is open on (someone deleted it). The
 * selection survives only while the release is still in the list.
 */
export function reconcileSelection(
  selectedReleaseNo: number | null,
  releases: readonly ReleaseSummary[],
): number | null {
  if (selectedReleaseNo === null) return null;
  return releases.some((release) => release.release_no === selectedReleaseNo)
    ? selectedReleaseNo
    : null;
}

// ----------------------------------------------------------------- notices

/** The `updated_by` of a change appmux makes on its own, with nobody signed in. */
const APPMUX_ACTOR = "appmux";

/**
 * What the person can do about a notice, drawn beside it: go to the section
 * where it is fixed, or press one of the app's buttons right there.
 */
export type NoticeAction =
  | { kind: "section"; section: AppSection; label: string }
  | { kind: "control"; control: "start" | "restart" | "update" };

export interface StatusNotice {
  /** Stable, for the React key and for tests. */
  id: string;
  tone: Tone;
  title: string;
  detail: string | null;
  action: NoticeAction;
  /** The release the notice is about, when it is about one. */
  releaseNo: number | null;
}

/** Most urgent first. A healthy, current app has none. */
export function statusNotices(app: AppDetail): StatusNotice[] {
  const notices: StatusNotice[] = [];
  const active = activeRelease(app);

  // appmux itself stopped the app because it now runs as another service principal (ADR 0022).
  // Pressing Start sets `updated_by` to the person, which is what ends this notice.
  if (
    app.config.desired_state === "stopped" &&
    app.config.updated_by === APPMUX_ACTOR
  ) {
    const principal =
      app.service_principal === null
        ? "the new service principal"
        : `the new service principal (${app.service_principal})`;
    notices.push({
      id: "service-principal-changed",
      tone: "warning",
      title: "appmux stopped this app",
      detail: `appmux now runs as another service principal, and the old one's access is gone. Give ${principal} what this app needs (a secret scope, a warehouse, table access), then press Start.`,
      action: { kind: "control", control: "start" },
      releaseNo: null,
    });
  }

  if (app.state === "crashed") {
    notices.push({
      id: "crashed",
      tone: "danger",
      title: "The app crashed",
      detail:
        "It stopped after repeated crashes and its route answers 503. Restart it once the cause is fixed, or activate a different release.",
      action: { kind: "section", section: "logs", label: "Read the logs" },
      releaseNo: null,
    });
  }

  if (app.restart_failure !== null) {
    // The server keeps the copy that was running when the new one fails, so a running app is
    // still served; after a crash there is no such copy and the sentence would be false.
    const serving =
      app.state === "running"
        ? " The copy that was running keeps serving."
        : "";
    notices.push({
      id: "restart-failed",
      tone: "warning",
      title: "The last restart did not come up",
      detail: `${app.restart_failure}.${serving}`,
      action: { kind: "control", control: "restart" },
      releaseNo: null,
    });
  }

  if (app.restart_to_apply) {
    notices.push({
      id: "restart-to-apply",
      tone: "warning",
      title: "Restart to apply the resource changes",
      detail:
        "Resources changed since the app last started; the running process still has the old ones.",
      action: { kind: "control", control: "restart" },
      releaseNo: null,
    });
  }

  const failedTarget = failedTargetRelease(app);
  if (failedTarget !== null) {
    const reason = failedTarget.status_reason ?? "no reason was recorded";
    const serving =
      active === null
        ? "No release is serving."
        : `Release ${active.release_no} keeps serving.`;
    notices.push({
      id: "failed-target",
      tone: "warning",
      title: `Release ${failedTarget.release_no} failed`,
      detail: `${reason}. ${serving}`,
      action: {
        kind: "section",
        section: "deployments",
        label: `See release ${String(failedTarget.release_no)}`,
      },
      releaseNo: failedTarget.release_no,
    });
  }

  const inProgress = inProgressRelease(app);
  if (inProgress !== null) {
    const status = releaseStatusPresentation(inProgress.status).label;
    notices.push({
      id: "in-progress",
      tone: "info",
      title: `Release ${inProgress.release_no} is ${status.toLowerCase()}`,
      detail:
        active === null
          ? "The route opens once its health check passes."
          : `Release ${active.release_no} keeps serving until the new release is healthy.`,
      action: {
        kind: "section",
        section: "deployments",
        label: "Follow it in Deployments",
      },
      releaseNo: inProgress.release_no,
    });
  }

  if (app.releases.length === 0) {
    notices.push({
      id: "no-release",
      tone: "info",
      title: "There is no release yet",
      detail:
        "Press Update to fetch the source folder and build the first release.",
      action: { kind: "control", control: "update" },
      releaseNo: null,
    });
  }

  const missingCount = app.requirements.items.filter(
    (requirement) => requirement.state === "missing",
  ).length;
  if (missingCount > 0) {
    const noun = pluralize(missingCount, "requirement");
    notices.push({
      id: "requirements-missing",
      tone: "warning",
      title: `${noun} ${missingCount === 1 ? "is" : "are"} missing`,
      detail: "Attach a resource for each one under Requirements in Resources.",
      action: {
        kind: "section",
        section: "resources",
        label: "Attach resources",
      },
      releaseNo: null,
    });
  }

  return notices;
}
