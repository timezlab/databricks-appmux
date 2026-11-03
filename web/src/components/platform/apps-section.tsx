"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { listApps } from "@/lib/api";
import type { PlatformView, RequestRow } from "@/lib/api";
import { appPagePath } from "@/lib/app/params";
import { carriedOutcome, dropCarriedOutcome } from "@/lib/app/settings";
import { formatMegabytes, pluralize } from "@/lib/format";
import { filterApps, platformAppRows } from "@/lib/platform/apps";
import type { PlatformAppRow } from "@/lib/platform/apps";
import { PLATFORM_APPS_PATH } from "@/lib/platform/sections";
import { pollData } from "@/lib/poll/data";
import { usePoll } from "@/hooks/use-poll";
import { DataTable } from "@/components/data-table";
import type { DataTableColumn } from "@/components/data-table";
import { EmptyState } from "@/components/empty-state";
import { ErrorNotice } from "@/components/error-notice";
import { Field } from "@/components/field";
import { Notice } from "@/components/notice";
import { RouteStrip } from "@/components/route-strip";
import { Section } from "@/components/section";
import { AppStateBadge } from "@/components/status-badge";
import { Input } from "@/components/ui/input";
import { usePlatformContext } from "./platform-context";

/** The lamps follow the app list; a state changes in seconds, like the platform's own poll. */
const APP_LIST_POLL_INTERVAL_MS = 10_000;

/** Where one app is changed: its Settings and Security sections, so the forms exist once. */
function AppLinks({ slug }: { slug: string }) {
  const settingsHref = appPagePath(slug, "settings");
  const securityHref = appPagePath(slug, "security");
  return (
    <span className="flex flex-wrap gap-x-3 gap-y-1">
      {settingsHref === null ? null : (
        <Link href={settingsHref} aria-label={`Settings of ${slug}`}>
          Settings
        </Link>
      )}
      {securityHref === null ? null : (
        <Link href={securityHref} aria-label={`Security of ${slug}`}>
          Security
        </Link>
      )}
    </span>
  );
}

const COLUMNS: readonly DataTableColumn<PlatformAppRow>[] = [
  {
    key: "app",
    header: "App",
    isRowHeader: true,
    cell: (row) => (
      <span className="flex flex-col items-start gap-1">
        <span className="wrap-break-word">{row.displayName}</span>
        <RouteStrip slug={row.slug} />
      </span>
    ),
  },
  {
    key: "state",
    header: "State",
    cell: (row) =>
      row.state === null ? (
        <span className="text-muted-foreground">Unknown</span>
      ) : (
        <AppStateBadge state={row.state} />
      ),
  },
  {
    key: "owner",
    header: "Owner",
    cell: (row) => <span className="wrap-break-word">{row.owner}</span>,
  },
  {
    key: "memory",
    header: "Memory budget",
    align: "end",
    cell: (row) => formatMegabytes(row.memoryBudgetMb),
  },
  {
    key: "requests",
    header: "Requests waiting",
    align: "end",
    cell: (row) =>
      row.pendingRequests === null ? (
        <span className="text-muted-foreground">Unknown</span>
      ) : (
        String(row.pendingRequests)
      ),
  },
  {
    key: "open",
    header: "Change",
    isLabelHiddenWhenStacked: true,
    cell: (row) => <AppLinks slug={row.slug} />,
  },
];

/**
 * Every registered app (design 10.1) as a table whose rows lead to the app's
 * Settings and Security sections, where its fields and egress rules are
 * changed. The state comes from the app list, which the platform view does not
 * carry. A delete on an app's Settings page lands here, and its outcome is
 * shown and read out once.
 */
export function AppsSection({
  view,
  requests,
}: {
  view: PlatformView;
  /** The request list the shell polls; `null` while it has not loaded. */
  requests: readonly RequestRow[] | null;
}) {
  const { announce } = usePlatformContext();
  const [query, setQuery] = useState("");
  const [arrival] = useState(() => carriedOutcome(PLATFORM_APPS_PATH));
  useEffect(() => {
    dropCarriedOutcome(PLATFORM_APPS_PATH);
    if (arrival !== null) announce(arrival.title);
  }, [arrival, announce]);
  const appList = usePoll((signal) => listApps({ signal }), {
    intervalMs: APP_LIST_POLL_INTERVAL_MS,
  });

  const rows = platformAppRows(
    filterApps(view.apps, query),
    pollData(appList)?.items ?? null,
    requests,
  );

  return (
    <Section title="Apps">
      <div className="flex flex-col gap-4">
        {arrival === null ? null : (
          <Notice tone="success" title={arrival.title}>
            {arrival.detail === null ? null : <p>{arrival.detail}</p>}
          </Notice>
        )}
        <p className="text-muted-foreground">
          An app&apos;s name, address, source folder, memory budget and owner
          are changed on its Settings page, and its egress rules on its Security
          page.
        </p>
        <div className="max-w-140">
          <Field label="Filter apps" hint="By slug, display name or owner.">
            {(control) => (
              <Input
                {...control}
                type="search"
                autoComplete="off"
                value={query}
                onChange={(event) => {
                  setQuery(event.target.value);
                }}
              />
            )}
          </Field>
        </div>
        {appList.status === "failed" ? (
          <ErrorNotice
            error={appList.error}
            context="Could not read the apps' states"
            onRetry={appList.refresh}
          />
        ) : null}
        <DataTable<PlatformAppRow>
          caption={`Registered apps (${pluralize(rows.length, "app")} shown of ${view.apps.length})`}
          rows={rows}
          rowKey={(row) => row.slug}
          empty={
            <EmptyState
              title={
                view.apps.length === 0
                  ? "No apps registered"
                  : "No app matches the filter"
              }
            >
              {view.apps.length === 0
                ? "Register one below, or approve a request in the inbox."
                : "Clear the filter to see every app."}
            </EmptyState>
          }
          columns={COLUMNS}
        />
      </div>
    </Section>
  );
}
