"use client";

import Link from "next/link";

import { getMetricsBreakdown } from "@/lib/api";
import { appPagePath } from "@/lib/app/params";
import {
  BREAKDOWN_OUTLINE,
  buildBreakdownModel,
} from "@/lib/dashboard/breakdown";
import type { BreakdownApp, BreakdownModel } from "@/lib/dashboard/breakdown";
import {
  breakdownQueryFor,
  metricsPollIntervalMs,
  metricsWindow,
  stepForRange,
} from "@/lib/dashboard/scope";
import type { TimeRange } from "@/lib/dashboard/scope";
import { usePoll } from "@/hooks/use-poll";
import { DataTable } from "@/components/data-table";
import type { DataTableColumn } from "@/components/data-table";
import { EmptyState } from "@/components/empty-state";
import { ErrorNotice } from "@/components/error-notice";
import { RelativeTime } from "@/components/relative-time";
import { RouteStrip } from "@/components/route-strip";
import { ChartFrame, PanelFrame } from "./chart-frame";
import { Swatch } from "./chart-legend";
import { MetricsError } from "./metrics-error";

/**
 * The app, named the way the charts' legend names it, and linked to its
 * Insights when it is one registered app. An app no longer registered keeps
 * its metrics but has no page, so its name is text and says why.
 */
function AppName({ app }: { app: BreakdownApp }) {
  const insights =
    app.slug === null || !app.isRegistered
      ? null
      : appPagePath(app.slug, "insights");
  return (
    <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
      <Swatch kind="columns" item={app} idPrefix="texture-apps-table" />
      {insights === null ? (
        <span>{app.label}</span>
      ) : (
        <Link href={insights}>{app.label}</Link>
      )}
      {app.slug === null || app.slug === app.label ? null : (
        <RouteStrip slug={app.slug} />
      )}
      {app.slug !== null && !app.isRegistered ? (
        <span className="text-muted-foreground">(no longer registered)</span>
      ) : null}
    </span>
  );
}

function appColumns(
  headers: readonly string[],
): DataTableColumn<BreakdownApp>[] {
  return headers.map((header, index) =>
    index === 0
      ? {
          key: "app",
          header,
          isRowHeader: true,
          cell: (app: BreakdownApp) => <AppName app={app} />,
        }
      : {
          key: String(index),
          header,
          align: "end",
          cell: (app: BreakdownApp) => app.cells[index - 1] ?? "",
        },
  );
}

function BreakdownFrames({ model }: { model: BreakdownModel }) {
  return (
    <>
      {model.charts.map((spec) => (
        <ChartFrame key={spec.id} status="ready" spec={spec} />
      ))}
      <PanelFrame title="Apps over the window">
        <DataTable
          caption="Apps over the window, busiest first"
          isCaptionHidden
          columns={appColumns(model.appHeaders)}
          rows={model.apps}
          rowKey={(app) => app.key}
        />
      </PanelFrame>
    </>
  );
}

/**
 * Polls `GET /metrics/breakdown` for the range, at the step the platform's
 * charts are read at, and draws it per app: requests, memory and server errors
 * stacked (the eight busiest apps and the rest), then a table of the apps with
 * their figures over the window, each linked to its Insights.
 */
export function BreakdownPanel({ range }: { range: TimeRange }) {
  const poll = usePoll(
    (signal) => {
      const step = stepForRange(range.minutes);
      return getMetricsBreakdown(
        breakdownQueryFor(metricsWindow(Date.now(), range.minutes), step.id),
        { signal },
      );
    },
    {
      intervalMs: metricsPollIntervalMs(range.minutes),
      resetKey: range.id,
    },
  );

  if (poll.status === "loading") {
    return (
      <div className="flex flex-col gap-4">
        {BREAKDOWN_OUTLINE.map((chart) => (
          <ChartFrame key={chart.id} status="loading" name={chart.name} />
        ))}
      </div>
    );
  }
  if (poll.status === "failed") {
    return (
      <div className="flex flex-col gap-4">
        <MetricsError
          error={poll.error}
          context="Could not load the metrics by app"
          onRetry={poll.refresh}
        />
        {BREAKDOWN_OUTLINE.map((chart) => (
          <ChartFrame key={chart.id} status="failed" name={chart.name} />
        ))}
      </div>
    );
  }

  const model = buildBreakdownModel(poll.data);
  return (
    <div className="flex flex-col gap-4">
      {poll.status === "stale" ? (
        <ErrorNotice
          error={poll.error}
          context="Could not refresh the metrics by app, showing the last ones received"
          onReload={poll.refresh}
          onRetry={poll.refresh}
        />
      ) : null}
      <p className="text-sm text-muted-foreground">
        {model.windowText}. Updated <RelativeTime value={poll.updatedAtMs} />.
      </p>
      {model.kind === "empty" ? (
        <EmptyState
          title={`No app traffic in the ${range.label.toLowerCase()}`}
        >
          {model.message}
        </EmptyState>
      ) : (
        <BreakdownFrames model={model} />
      )}
    </div>
  );
}
