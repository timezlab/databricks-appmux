"use client";

import { getMetrics } from "@/lib/api";
import { buildDashboardModel } from "@/lib/dashboard/model";
import type { SeriesModel } from "@/lib/dashboard/model";
import {
  metricsPollIntervalMs,
  metricsQueryFor,
  metricsWindow,
  scopeKey,
} from "@/lib/dashboard/scope";
import type { DashboardScope, TimeRange } from "@/lib/dashboard/scope";
import { usePoll } from "@/hooks/use-poll";
import { Button } from "@/components/button";
import { DataTable } from "@/components/data-table";
import { EmptyState } from "@/components/empty-state";
import { ErrorNotice } from "@/components/error-notice";
import { Loading } from "@/components/loading";
import { Notice } from "@/components/notice";
import { RelativeTime } from "@/components/relative-time";
import { Section } from "@/components/section";
import { AppsComparison } from "./apps-comparison";
import { ChartFigure } from "./chart-figure";
import { StatTiles } from "./stat-tiles";

export interface MetricsPanelProps {
  scope: DashboardScope;
  range: TimeRange;
  isSelectable: (slug: string) => boolean;
  onSelectApp: (slug: string) => void;
}

/**
 * Polls `GET /metrics` for the scope and range and draws the answer. The poll
 * key names scope and range, so changing either drops the old charts at once
 * and asks again. A failed poll after a good one keeps the last charts under an
 * error notice.
 */
export function MetricsPanel({
  scope,
  range,
  isSelectable,
  onSelectApp,
}: MetricsPanelProps) {
  const poll = usePoll(
    (signal) =>
      getMetrics(
        metricsQueryFor(scope, metricsWindow(Date.now(), range.minutes)),
        { signal },
      ),
    {
      intervalMs: metricsPollIntervalMs(scope, range.minutes),
      resetKey: `${scopeKey(scope)}|${range.id}`,
    },
  );

  if (poll.status === "loading") return <Loading label="Loading metrics" />;
  if (poll.status === "failed") {
    return (
      <ErrorNotice
        error={poll.error}
        context="Could not load the metrics"
        onReload={poll.refresh}
      />
    );
  }

  const model = buildDashboardModel(poll.data, scope);
  return (
    <div className="flex flex-col gap-4">
      {poll.status === "stale" ? (
        <ErrorNotice
          error={poll.error}
          context="Could not refresh the metrics, showing the last ones received"
          onReload={poll.refresh}
        />
      ) : null}
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <p className="text-muted-foreground">
          {model.windowText}. Updated <RelativeTime value={poll.updatedAtMs} />.
        </p>
        <Button
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
        <EmptyState title={model.title}>{model.message}</EmptyState>
      ) : null}
      {model.kind === "apps" ? (
        <Section title="Apps compared">
          <AppsComparison
            rows={model.rows}
            isSelectable={isSelectable}
            onSelectApp={onSelectApp}
          />
        </Section>
      ) : null}
      {model.kind === "series" ? <SeriesSections model={model} /> : null}
    </div>
  );
}

function SeriesSections({ model }: { model: SeriesModel }) {
  return (
    <>
      <Section title="Summary">
        <StatTiles tiles={model.tiles} />
      </Section>
      {model.charts.map((spec) => (
        <Section key={spec.id} title={spec.title}>
          <ChartFigure spec={spec} />
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
        </Section>
      ))}
      <Section title="Other figures">
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
      </Section>
    </>
  );
}
