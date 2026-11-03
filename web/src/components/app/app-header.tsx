"use client";

import { useState } from "react";
import type { ReactNode } from "react";

import { createRelease, restartApp, startApp, stopApp } from "@/lib/api";
import type { AppDetail } from "@/lib/api";
import { appActionAvailability } from "@/lib/app/actions";
import type { Actor, AppAction } from "@/lib/app/actions";
import { appControlMessage, updateOutcomeMessage } from "@/lib/app/outcomes";
import type { AppControlAction } from "@/lib/app/outcomes";
import { unavailableReasons } from "@/lib/app/reasons";
import { activeRelease, statusNotices } from "@/lib/app/status";
import { formatMegabytes } from "@/lib/format";
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
import type { AppActions } from "./use-app-actions";

export interface AppHeaderProps {
  app: AppDetail;
  actor: Actor | null;
  actions: AppActions;
  /** Reads the app again: offered after a 409. */
  onReload: () => void;
}

function controlCall(action: AppControlAction, slug: string) {
  switch (action) {
    case "start":
      return startApp(slug);
    case "stop":
      return stopApp(slug);
    case "restart":
      return restartApp(slug);
  }
}

/** One fact of the status list: a small label over its value. */
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

/** The Overview section (the earlier Status page): what needs attention first, then the app-level buttons and the facts. */
export function AppStatus({ app, actor, actions, onReload }: AppHeaderProps) {
  const [failure, setFailure] = useState<{ error: unknown } | null>(null);

  const isMeantToRun = app.config.desired_state === "running";
  const availability = (action: AppAction) =>
    appActionAvailability(action, app, actor);
  const update = availability("update");
  const restart = availability("restart");
  const toggle = availability(isMeantToRun ? "stop" : "start");
  const active = activeRelease(app);

  async function runUpdate(): Promise<void> {
    setFailure(null);
    const result = await actions.run(
      "update",
      () => createRelease(app.slug),
      updateOutcomeMessage,
    );
    if (!result.ok) setFailure({ error: result.error });
  }

  async function runControl(action: AppControlAction): Promise<void> {
    setFailure(null);
    const result = await actions.run(
      action,
      () => controlCall(action, app.slug),
      appControlMessage(action),
    );
    if (!result.ok) setFailure({ error: result.error });
  }

  const reasons = unavailableReasons([
    { label: "Update", availability: update },
    { label: isMeantToRun ? "Stop" : "Start", availability: toggle },
    { label: "Restart", availability: restart },
  ]);

  return (
    <>
      <Section
        title="Status"
        actions={
          <>
            {isMeantToRun ? (
              <ConfirmButton
                label="Stop"
                prompt={`Stop ${app.slug}? Its route closes until it is started again.`}
                confirmLabel={`Yes, stop ${app.slug}`}
                isBusy={actions.stateOf("stop") === "busy"}
                busyLabel="Stopping"
                isDisabled={
                  !toggle.isAllowed || actions.stateOf("stop") === "blocked"
                }
                onConfirm={() => {
                  void runControl("stop");
                }}
              />
            ) : (
              <Button
                isBusy={actions.stateOf("start") === "busy"}
                busyLabel="Starting"
                disabled={
                  !toggle.isAllowed || actions.stateOf("start") === "blocked"
                }
                onClick={() => {
                  void runControl("start");
                }}
              >
                Start
              </Button>
            )}
            <ConfirmButton
              label="Restart"
              prompt={`Restart ${app.slug}? It stops and starts again from its active release.`}
              confirmLabel={`Yes, restart ${app.slug}`}
              isBusy={actions.stateOf("restart") === "busy"}
              busyLabel="Restarting"
              isDisabled={
                !restart.isAllowed || actions.stateOf("restart") === "blocked"
              }
              onConfirm={() => {
                void runControl("restart");
              }}
            />
            <Button
              variant="primary"
              isBusy={actions.stateOf("update") === "busy"}
              busyLabel="Updating"
              disabled={
                !update.isAllowed || actions.stateOf("update") === "blocked"
              }
              onClick={() => {
                void runUpdate();
              }}
            >
              Update
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-4">
          {failure === null ? null : (
            <ErrorNotice
              error={failure.error}
              context="The action did not go through"
              onReload={onReload}
            />
          )}
          {statusNotices(app).map((notice) => (
            <Notice key={notice.id} tone={notice.tone} title={notice.title}>
              {notice.detail === null ? null : <p>{notice.detail}</p>}
            </Notice>
          ))}
          {reasons.length === 0 ? null : (
            <ul className="text-sm text-muted-foreground">
              {reasons.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          )}

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
                : `Release ${active.release_no}${active.name === null ? "" : `, ${active.name}`}`}
            </Fact>
            <Fact label="Runtime">{app.runtime ?? "Not detected yet"}</Fact>
            <Fact label="Memory budget">
              {formatMegabytes(app.memory_budget_mb)}
            </Fact>
            <Fact label="Source folder">
              <span className="font-mono text-[0.9em]">{app.source_path}</span>
            </Fact>
            <Fact label="Last changed">
              <RelativeTime value={app.updated_at} /> by {app.updated_by}
            </Fact>
          </dl>
        </div>
      </Section>
    </>
  );
}
