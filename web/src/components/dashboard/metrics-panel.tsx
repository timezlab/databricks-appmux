"use client";

import { LoaderCircleIcon } from "lucide-react";
import type { ReactNode } from "react";

import { getMetrics } from "@/lib/api";
import type { MetricsResponse } from "@/lib/api/schemas";
import { buildDashboardModel, chartOutline } from "@/lib/dashboard/model";
import type { SeriesModel } from "@/lib/dashboard/model";
import {
  PLATFORM_APP_SLUG,
  longerRange,
  metricsPollIntervalMs,
  metricsQueryFor,
  metricsWindow,
  previousWindow,
  stepForRange,
} from "@/lib/dashboard/scope";
import type {
  DashboardScope,
  TimeRange,
  TimeRangeId,
} from "@/lib/dashboard/scope";
import { usePoll } from "@/hooks/use-poll";
import { Button } from "@/components/button";
import { DataTable } from "@/components/data-table";
import { EmptyState } from "@/components/empty-state";
import { ErrorNotice } from "@/components/error-notice";
import { Notice } from "@/components/notice";
import { RelativeTime } from "@/components/relative-time";
import { ChartFrame, PanelFrame } from "./chart-frame";
import { FiguresStrip, FiguresStripSkeleton } from "./figures-strip";
import { MetricsError } from "./metrics-error";

export interface MetricsPanelProps {
  scope: DashboardScope;
  range: TimeRange;
  /** Offered from the empty state: "Show the last 6 hours". */
  onRangeChange: (id: TimeRangeId) => void;
}

interface MetricsAnswer {
  current: MetricsResponse;
  /** The window before, for the change figures; `null` when it is older than the data kept, or could not be read. */
  previous: MetricsResponse | null;
  stepMinutes: number;
}

/**
 * The window of the range and the one before it, read at the finest step that
 * fits, in parallel. The window before only adds the change figures: when it
 * fails they are left out and the charts of the window still show, so its
 * error is not the page's (an abort still is, so a left page stops at once).
 */
async function loadMetrics(
  scope: DashboardScope,
  range: TimeRange,
  signal: AbortSignal,
): Promise<MetricsAnswer> {
  const step = stepForRange(range.minutes);
  const window = metricsWindow(Date.now(), range.minutes);
  const before = previousWindow(window);
  const [current, previous] = await Promise.all([
    getMetrics(metricsQueryFor(scope, window, step.id), { signal }),
    before === null
      ? null
      : getMetrics(metricsQueryFor(scope, before, step.id), { signal }).catch(
          (error: unknown) => {
            if (signal.aborted) throw error;
            return null;
          },
        ),
  ]);
  return { current, previous, stepMinutes: step.minutes };
}

function LoadingStatus({ children }: { children: ReactNode }) {
  return (
    <p
      role="status"
      className="flex items-center gap-2 text-sm text-muted-foreground"
    >
      <LoaderCircleIcon
        className="size-4 shrink-0 animate-spinner"
        aria-hidden="true"
      />
      <span>{children}</span>
    </p>
  );
}

/**
 * Polls `GET /metrics` for the scope and range and draws the answer: the
 * figures strip, then one framed chart after another. The poll key names scope
 * and range, so changing either drops the old charts at once and asks again;
 * until the answer comes, every frame keeps its place with a skeleton. A failed
 * poll after a good one keeps the last charts under an error notice.
 */
export function MetricsPanel({
  scope,
  range,
  onRangeChange,
}: MetricsPanelProps) {
  const poll = usePoll((signal) => loadMetrics(scope, range, signal), {
    intervalMs: metricsPollIntervalMs(range.minutes),
    resetKey: `${scope.kind === "platform" ? PLATFORM_APP_SLUG : scope.slug}|${range.id}`,
  });
  const isPlatform = scope.kind === "platform";
  const outline = chartOutline(scope);

  if (poll.status === "loading") {
    return (
      <div className="flex flex-col gap-4">
        <LoadingStatus>
          Loading the metrics of the {range.label.toLowerCase()}…
        </LoadingStatus>
        <FiguresStripSkeleton isPlatform={isPlatform} />
        {outline.map((chart) => (
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
          context="Could not load the metrics"
          onRetry={poll.refresh}
        />
        {outline.map((chart) => (
          <ChartFrame key={chart.id} status="failed" name={chart.name} />
        ))}
      </div>
    );
  }

  const model = buildDashboardModel(poll.data.current, scope, {
    stepMinutes: poll.data.stepMinutes,
    previous: poll.data.previous,
    span: range.span,
  });
  const longer = longerRange(range.id);
  return (
    <div className="flex flex-col gap-4">
      {poll.status === "stale" ? (
        <ErrorNotice
          error={poll.error}
          context="Could not refresh the metrics, showing the last ones received"
          onReload={poll.refresh}
          onRetry={poll.refresh}
        />
      ) : null}
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <p className="text-sm text-muted-foreground">
          {model.windowText}. Updated <RelativeTime value={poll.updatedAtMs} />.
        </p>
        <Button
          size="small"
          isBusy={poll.isFetching}
          busyLabel="Refreshing"
          onClick={poll.refresh}
        >
          Refresh
        </Button>
      </div>
      {model.notices.map((notice) => (
        <Notice key={notice.key} tone={notice.tone} title={notice.title}>
          <p>{notice.detail}</p>
        </Notice>
      ))}
      {model.kind === "empty" ? (
        <EmptyState
          title={`No traffic in the ${range.label.toLowerCase()}`}
          action={
            longer === null ? undefined : (
              <Button
                onClick={() => {
                  onRangeChange(longer.id);
                }}
              >
                Show the {longer.label.toLowerCase()}
              </Button>
            )
          }
        >
          {model.message}
        </EmptyState>
      ) : (
        <SeriesFrames model={model} />
      )}
    </div>
  );
}

function SeriesFrames({ model }: { model: SeriesModel }) {
  return (
    <>
      <FiguresStrip figures={model.figures} />
      {model.charts.map((spec) => (
        <ChartFrame key={spec.id} status="ready" spec={spec}>
          {spec.id === "egress" && model.drops.length > 0 ? (
            <DataTable
              caption="Dropped packets by egress rule"
              layout="scroll"
              columns={[
                {
                  key: "rule",
                  header: "Rule",
                  isRowHeader: true,
                  cell: (row) => (
                    <span className="font-mono text-[0.9em] wrap-break-word">
                      {row.rule}
                    </span>
                  ),
                },
                {
                  key: "count",
                  header: "Drops",
                  align: "end",
                  cell: (row) => String(row.count),
                },
              ]}
              rows={model.drops}
              rowKey={(row) => row.rule}
            />
          ) : null}
        </ChartFrame>
      ))}
      <PanelFrame title="Other figures">
        <DataTable
          caption="Other figures over the window"
          isCaptionHidden
          layout="scroll"
          columns={[
            {
              key: "label",
              header: "Figure",
              isRowHeader: true,
              cell: (row) => row.label,
            },
            {
              key: "value",
              header: "Value",
              align: "end",
              cell: (row) => row.value,
            },
          ]}
          rows={model.facts}
          rowKey={(row) => row.key}
        />
      </PanelFrame>
    </>
  );
}
