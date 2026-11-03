"use client";

import { useId } from "react";
import type { ReactNode } from "react";

import type { AppDetail } from "@/lib/api";
import { appActionAvailability } from "@/lib/app/actions";
import type { Actor, Availability } from "@/lib/app/actions";
import { unavailableReasons } from "@/lib/app/reasons";
import { activeRelease, statusNotices } from "@/lib/app/status";
import type { NoticeAction, StatusNotice } from "@/lib/app/status";
import { formatMegabytes } from "@/lib/format";
import { pollData } from "@/lib/poll/data";
import { AppLink } from "@/components/app-link";
import { Button } from "@/components/button";
import { ConfirmButton } from "@/components/confirm-button";
import { ErrorNotice } from "@/components/error-notice";
import { Notice } from "@/components/notice";
import { PageHeader } from "@/components/page-header";
import { RelativeTime } from "@/components/relative-time";
import { RouteStrip } from "@/components/route-strip";
import { Section } from "@/components/section";
import { AppStateBadge } from "@/components/status-badge";
import { UpdateButton, useAppControls } from "./app-controls";
import type { AppControls } from "./app-controls";
import { AppSectionLink } from "./app-section-link";
import { FailureHint } from "./failure-hint";
import type { AppActions } from "./use-app-actions";
import { useReleaseDetail } from "./use-release-detail";

export interface AppHeaderProps {
  app: AppDetail;
  actor: Actor | null;
  actions: AppActions;
  /** Reads the app again: offered after a 409. */
  onReload: () => void;
}

/** One fact of the overview: a small label over its value. */
function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <dt className="text-sm text-muted-foreground">{label}</dt>
      <dd className="min-w-0 wrap-break-word">{children}</dd>
    </div>
  );
}

/**
 * The top of every page of an app: its name, slug and owner, its state, and the
 * way to open it. The name is the page's `<h1>` in every section; the sidebar
 * under it says which section this is.
 */
export function AppTitle({ app }: { app: AppDetail }) {
  return (
    <PageHeader
      title={app.display_name}
      route={<RouteStrip slug={app.slug} />}
      description={
        <span className="flex flex-wrap items-center gap-x-4 gap-y-1">
          <AppStateBadge state={app.state} />
          <span>
            <span className="font-mono text-[0.9em] wrap-break-word">
              {app.slug}
            </span>
            , owned by <span className="wrap-break-word">{app.owner}</span>
          </span>
        </span>
      }
      actions={
        <AppLink slug={app.slug} buttonVariant="primary">
          Open app
        </AppLink>
      }
    />
  );
}

interface ControlAvailability {
  start: Availability;
  stop: Availability;
  restart: Availability;
  update: Availability;
}

/** Start, Stop, Restart and Update in one row; the ones that are off say why under it. */
function ControlRow({
  app,
  actions,
  controls,
  availability,
}: {
  app: AppDetail;
  actions: AppActions;
  controls: AppControls;
  availability: ControlAvailability;
}) {
  const reasons = unavailableReasons([
    { label: "Start", availability: availability.start },
    { label: "Stop", availability: availability.stop },
    { label: "Restart", availability: availability.restart },
    { label: "Update", availability: availability.update },
  ]);
  const isHeld = (key: string) => actions.stateOf(key) === "blocked";

  return (
    <div className="flex flex-col gap-2">
      <div
        role="group"
        aria-label="App controls"
        className="flex flex-wrap items-center gap-2"
      >
        <Button
          isBusy={actions.stateOf("start") === "busy"}
          busyLabel="Starting"
          disabled={!availability.start.isAllowed || isHeld("start")}
          onClick={() => {
            controls.runControl("start");
          }}
        >
          Start
        </Button>
        <ConfirmButton
          label="Stop"
          prompt={`Stop ${app.slug}? Its route closes until it is started again.`}
          confirmLabel={`Yes, stop ${app.slug}`}
          isBusy={actions.stateOf("stop") === "busy"}
          busyLabel="Stopping"
          isDisabled={!availability.stop.isAllowed || isHeld("stop")}
          onConfirm={() => {
            controls.runControl("stop");
          }}
        />
        <ConfirmButton
          label="Restart"
          prompt={`Restart ${app.slug}? It stops and starts again from its active release.`}
          confirmLabel={`Yes, restart ${app.slug}`}
          isBusy={actions.stateOf("restart") === "busy"}
          busyLabel="Restarting"
          isDisabled={!availability.restart.isAllowed || isHeld("restart")}
          onConfirm={() => {
            controls.runControl("restart");
          }}
        />
        <UpdateButton
          availability={availability.update}
          actions={actions}
          onUpdate={controls.runUpdate}
        />
      </div>
      {reasons.length === 0 ? null : (
        <ul className="text-sm text-muted-foreground">
          {reasons.map((reason) => (
            <li key={reason}>{reason}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * The action beside a notice. A control here has its own words ("Restart
 * now"), so it is not a second button with the name of one in the row above.
 */
function NoticeActionControl({
  app,
  action,
  actions,
  controls,
  availability,
}: {
  app: AppDetail;
  action: NoticeAction;
  actions: AppActions;
  controls: AppControls;
  availability: ControlAvailability;
}) {
  if (action.kind === "section") {
    return (
      <AppSectionLink
        slug={app.slug}
        section={action.section}
        buttonVariant="secondary"
      >
        {action.label}
      </AppSectionLink>
    );
  }
  if (action.control === "restart") {
    return (
      <ConfirmButton
        label="Restart now"
        size="small"
        prompt={`Restart ${app.slug}? It stops and starts again with the new resources.`}
        confirmLabel={`Yes, restart ${app.slug}`}
        isBusy={actions.stateOf("restart") === "busy"}
        busyLabel="Restarting"
        isDisabled={
          !availability.restart.isAllowed ||
          actions.stateOf("restart") === "blocked"
        }
        onConfirm={() => {
          controls.runControl("restart");
        }}
      />
    );
  }
  return (
    <Button
      size="small"
      isBusy={actions.stateOf("update") === "busy"}
      busyLabel="Updating"
      disabled={
        !availability.update.isAllowed ||
        actions.stateOf("update") === "blocked"
      }
      onClick={controls.runUpdate}
    >
      Fetch the first release
    </Button>
  );
}

/** "Needs attention": each notice with what to do about it, or a line saying there is nothing. */
function AttentionList({
  app,
  notices,
  failureHint,
  actions,
  controls,
  availability,
}: {
  app: AppDetail;
  notices: readonly StatusNotice[];
  failureHint: string | null;
  actions: AppActions;
  controls: AppControls;
  availability: ControlAvailability;
}) {
  const headingId = useId();
  return (
    <div className="flex flex-col gap-2">
      <h3 id={headingId}>Needs attention</h3>
      {notices.length === 0 ? (
        <p className="text-muted-foreground">
          Nothing needs attention: the app runs the release it should.
        </p>
      ) : (
        <ul aria-labelledby={headingId} className="flex flex-col gap-2">
          {notices.map((notice) => (
            <li key={notice.id}>
              <Notice tone={notice.tone} title={notice.title}>
                <div className="flex flex-col gap-2">
                  {notice.detail === null ? null : (
                    <p className="wrap-break-word">{notice.detail}</p>
                  )}
                  {notice.id === "failed-target" && failureHint !== null ? (
                    <FailureHint slug={app.slug} hint={failureHint} />
                  ) : null}
                  <div>
                    <NoticeActionControl
                      app={app}
                      action={notice.action}
                      actions={actions}
                      controls={controls}
                      availability={availability}
                    />
                  </div>
                </div>
              </Notice>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * The Overview section: the app's buttons, what needs attention with the way
 * to fix each, then the facts. A failed target is read once more for the
 * server's hint (the release list does not carry it), so a Node.js app that
 * cannot start in its memory budget says so here, with the way to Settings.
 */
export function AppOverview({ app, actor, actions, onReload }: AppHeaderProps) {
  const controls = useAppControls(app, actions);
  const notices = statusNotices(app);
  const failedTargetNo =
    notices.find((notice) => notice.id === "failed-target")?.releaseNo ?? null;
  const failedTarget = useReleaseDetail(
    app.slug,
    failedTargetNo,
    failedTargetNo !== null,
  );
  // A hint that cannot be read is left out: the notice and the release page still say what failed.
  const failureHint = pollData(failedTarget)?.failure_hint ?? null;

  const isMeantToRun = app.config.desired_state === "running";
  const availability: ControlAvailability = {
    start: appActionAvailability("start", app, actor),
    stop: appActionAvailability("stop", app, actor),
    restart: appActionAvailability("restart", app, actor),
    update: appActionAvailability("update", app, actor),
  };
  const active = activeRelease(app);

  return (
    <Section title="Overview">
      <div className="flex flex-col gap-6">
        <ControlRow
          app={app}
          actions={actions}
          controls={controls}
          availability={availability}
        />
        {controls.failure === null ? null : (
          <ErrorNotice
            error={controls.failure.error}
            context="The action did not go through"
            onReload={onReload}
          />
        )}
        <AttentionList
          app={app}
          notices={notices}
          failureHint={failureHint}
          actions={actions}
          controls={controls}
          availability={availability}
        />
        <dl className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,14rem),1fr))] gap-x-4 gap-y-4">
          <Fact label="State">
            <AppStateBadge state={app.state} />{" "}
            <span className="text-muted-foreground">
              {isMeantToRun ? "meant to run" : "stopped by an admin"}
            </span>
          </Fact>
          <Fact label="Active release">
            {active === null
              ? "None yet"
              : `Release ${String(active.release_no)}${active.name === null ? "" : `, ${active.name}`}`}
          </Fact>
          <Fact label="Runtime">{app.runtime ?? "Not detected yet"}</Fact>
          <Fact label="Memory budget">
            {formatMegabytes(app.memory_budget_mb)}
          </Fact>
          <Fact label="Source folder">
            <span className="font-mono text-[0.9em]">{app.source_path}</span>
          </Fact>
          <Fact label="Owner">{app.owner}</Fact>
          <Fact label="Last changed">
            <RelativeTime value={app.updated_at} /> by {app.updated_by}
          </Fact>
        </dl>
      </div>
    </Section>
  );
}
