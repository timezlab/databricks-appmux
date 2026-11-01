"use client";

import { useState } from "react";

import { getMe, listApps } from "@/lib/api";
import type { AppSummary } from "@/lib/api/schemas";
import {
  DEFAULT_RANGE_ID,
  TIME_RANGES,
  parseScopeKey,
  resolveScopeKey,
  scopeKey,
  scopeOptions,
  timeRangeById,
} from "@/lib/dashboard/scope";
import type { TimeRangeId } from "@/lib/dashboard/scope";
import { usePoll } from "@/hooks/use-poll";
import { EmptyState } from "@/components/empty-state";
import { ErrorNotice } from "@/components/error-notice";
import { Loading } from "@/components/loading";
import { PageHeader } from "@/components/page-header";
import { DashboardControls } from "./dashboard-controls";
import { MetricsPanel } from "./metrics-panel";

/** Who the person is and which apps they may chart changes rarely. */
const DIRECTORY_POLL_INTERVAL_MS = 30_000;

/** Used only if the range state holds an id the list does not (it cannot: the select offers the list). */
const FALLBACK_RANGE = TIME_RANGES[1];

async function loadDirectory(signal: AbortSignal) {
  const [me, apps] = await Promise.all([
    getMe({ signal }),
    listApps({ signal }),
  ]);
  return { isPlatformAdmin: me.is_platform_admin, apps: apps.items };
}

/**
 * The dashboard page. The server renders only the header and "Loading"; the
 * identity, the app list and the metrics are all read in the browser after the
 * page mounts.
 */
export function DashboardView() {
  const directory = usePoll(loadDirectory, {
    intervalMs: DIRECTORY_POLL_INTERVAL_MS,
    resetKey: "directory",
  });
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [rangeId, setRangeId] = useState<TimeRangeId>(DEFAULT_RANGE_ID);

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Dashboard"
        description="Requests, response time, memory and dropped egress packets, per app and for the platform."
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
        <DashboardBody
          isPlatformAdmin={directory.data.isPlatformAdmin}
          apps={directory.data.apps}
          selectedKey={selectedKey}
          onSelectKey={setSelectedKey}
          rangeId={rangeId}
          onRangeChange={setRangeId}
        />
      ) : null}
    </div>
  );
}

interface DashboardBodyProps {
  isPlatformAdmin: boolean;
  apps: readonly AppSummary[];
  selectedKey: string | null;
  onSelectKey: (key: string) => void;
  rangeId: TimeRangeId;
  onRangeChange: (id: TimeRangeId) => void;
}

function DashboardBody({
  isPlatformAdmin,
  apps,
  selectedKey,
  onSelectKey,
  rangeId,
  onRangeChange,
}: DashboardBodyProps) {
  const options = scopeOptions({ isPlatformAdmin, apps });
  const activeKey = resolveScopeKey(selectedKey, options);
  const scope = activeKey === null ? null : parseScopeKey(activeKey);
  if (activeKey === null || scope === null) {
    return (
      <EmptyState title="No dashboard to show">
        Charts are for platform admins and for people who can manage an app.
      </EmptyState>
    );
  }
  const range = timeRangeById(rangeId) ?? FALLBACK_RANGE;

  return (
    <>
      <DashboardControls
        options={options}
        scopeKey={activeKey}
        onScopeChange={onSelectKey}
        rangeId={rangeId}
        onRangeChange={onRangeChange}
      />
      <MetricsPanel
        scope={scope}
        range={range}
        isSelectable={(slug) =>
          options.some(
            (option) => option.key === scopeKey({ kind: "app", slug }),
          )
        }
        onSelectApp={(slug) => {
          onSelectKey(scopeKey({ kind: "app", slug }));
        }}
      />
    </>
  );
}
