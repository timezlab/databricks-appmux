"use client";

import Link from "next/link";
import { useState } from "react";

import { getMe, listApps } from "@/lib/api";
import type { AppSummary } from "@/lib/api/schemas";
import { appPagePath } from "@/lib/app/params";
import {
  DEFAULT_RANGE_ID,
  TIME_RANGES,
  managedApps,
  timeRangeById,
} from "@/lib/dashboard/scope";
import type { DashboardScope, TimeRangeId } from "@/lib/dashboard/scope";
import { usePoll } from "@/hooks/use-poll";
import { EmptyState } from "@/components/empty-state";
import { ErrorNotice } from "@/components/error-notice";
import { Loading } from "@/components/loading";
import { PageHeader } from "@/components/page-header";
import { RouteStrip } from "@/components/route-strip";
import { Section } from "@/components/section";
import { BreakdownPanel } from "./breakdown-panel";
import { MetricsPanel } from "./metrics-panel";
import { RangeControl } from "./range-control";

/** Who the person is and which apps they may chart changes rarely. */
const DIRECTORY_POLL_INTERVAL_MS = 30_000;

/** Used only if the range state holds an id the list does not (it cannot: the control offers the list). */
const FALLBACK_RANGE = TIME_RANGES[1];

const PLATFORM_SCOPE: DashboardScope = { kind: "platform" };

async function loadDirectory(signal: AbortSignal) {
  const [me, apps] = await Promise.all([
    getMe({ signal }),
    listApps({ signal }),
  ]);
  return { isPlatformAdmin: me.is_platform_admin, apps: apps.items };
}

/**
 * The dashboard page. A platform admin sees the platform's charts and the same
 * window broken down by app; anyone else sees the apps they manage, each linked
 * to its Insights (the same charts, for one app). The server renders only the
 * header and "Loading"; the identity, the app list and the metrics are all read
 * in the browser after the page mounts.
 */
export function DashboardView() {
  const directory = usePoll(loadDirectory, {
    intervalMs: DIRECTORY_POLL_INTERVAL_MS,
    resetKey: "directory",
  });

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Dashboard"
        description="The platform's requests, response time, memory and dropped egress packets, then the same window app by app."
      />
      {directory.status === "loading" ? (
        <Loading label="Loading the dashboard" />
      ) : null}
      {directory.status === "failed" ? (
        <ErrorNotice
          error={directory.error}
          context="Could not load who you are and your apps"
          onReload={directory.refresh}
        />
      ) : null}
      {directory.status === "ready" || directory.status === "stale" ? (
        directory.data.isPlatformAdmin ? (
          <PlatformDashboard />
        ) : (
          <ManagedApps apps={directory.data.apps} />
        )
      ) : null}
    </div>
  );
}

function PlatformDashboard() {
  const [rangeId, setRangeId] = useState<TimeRangeId>(DEFAULT_RANGE_ID);
  const range = timeRangeById(rangeId) ?? FALLBACK_RANGE;
  return (
    <>
      <RangeControl rangeId={rangeId} onRangeChange={setRangeId} />
      <Section title="Platform">
        <MetricsPanel
          scope={PLATFORM_SCOPE}
          range={range}
          onRangeChange={setRangeId}
        />
      </Section>
      <Section title="By app">
        <BreakdownPanel range={range} />
      </Section>
    </>
  );
}

/** The apps someone who is not a platform admin may chart, each linked to its Insights. */
function ManagedApps({ apps }: { apps: readonly AppSummary[] }) {
  const managed = managedApps(apps);
  if (managed.length === 0) {
    return (
      <EmptyState title="No dashboard to show">
        The platform&apos;s charts are for platform admins, and an app&apos;s
        Insights are for the people who manage it. You manage no app yet.
      </EmptyState>
    );
  }
  return (
    <Section title="Insights of your apps">
      <p className="text-muted-foreground">
        The platform&apos;s charts are for platform admins. Each app you manage
        has the same charts for itself on its Insights page.
      </p>
      <ul className="flex flex-col divide-y rounded-lg border bg-card">
        {managed.map((app) => {
          const insights = appPagePath(app.slug, "insights");
          return (
            <li
              key={app.slug}
              className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-3"
            >
              {insights === null ? (
                <span className="font-medium">{app.display_name}</span>
              ) : (
                <Link href={insights} className="font-medium">
                  {app.display_name}
                  <span className="sr-only">: Insights</span>
                </Link>
              )}
              <RouteStrip slug={app.slug} />
            </li>
          );
        })}
      </ul>
    </Section>
  );
}
