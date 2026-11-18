import { Notice } from "@/components/notice";
import type { AppSection } from "@/lib/app/params";
import type {
  DeployProgress,
  DeployStepState,
} from "@/lib/app/deploy-progress";
import { toneMark } from "@/lib/status-presentation";
import { AppSectionLink } from "./app-section-link";

const STEP_MARK: Record<DeployStepState, string> = {
  done: toneMark("success"),
  current: toneMark("info"),
  todo: toneMark("neutral"),
};

// Spoken in place of the mark, which is hidden from screen readers.
const STEP_SPOKEN: Record<DeployStepState, string> = {
  done: ", done",
  current: ", in progress",
  todo: ", not yet",
};

export interface DeployBannerProps {
  slug: string;
  progress: DeployProgress;
  /** The section on show: the banner points to the other places that follow the Deploy. */
  section: AppSection | null;
}

/**
 * Shown above every section of the app while a release is being built and
 * started. It is drawn from the polled app, so a reload brings it back: the
 * Deploy does not depend on the page that pressed the button.
 */
export function DeployBanner({ slug, progress, section }: DeployBannerProps) {
  return (
    <Notice tone="info" title={`Deploying release ${progress.releaseNo}`}>
      <div className="flex flex-col gap-2">
        <ol
          aria-label="Steps of the deploy"
          className="flex flex-wrap gap-x-4 gap-y-1"
        >
          {progress.steps.map((step) => (
            <li
              key={step.label}
              aria-current={step.state === "current" ? "step" : undefined}
              className={step.state === "current" ? "font-semibold" : undefined}
            >
              <span aria-hidden="true">{STEP_MARK[step.state]}</span>{" "}
              {step.label}
              <span className="sr-only">{STEP_SPOKEN[step.state]}</span>
            </li>
          ))}
        </ol>
        <p>{progress.outlook}</p>
        {section === "logs" && !progress.isBuildOutputRecorded ? (
          <p>
            The build output is not recorded. The log shows the copy that was
            running; the new copy&apos;s lines appear once it starts.
          </p>
        ) : null}
        <p className="flex flex-wrap gap-x-4">
          {section === "deployments" ? null : (
            <AppSectionLink slug={slug} section="deployments">
              See the release
            </AppSectionLink>
          )}
          {section === "logs" ? null : (
            <AppSectionLink slug={slug} section="logs">
              Read the logs
            </AppSectionLink>
          )}
        </p>
      </div>
    </Notice>
  );
}
