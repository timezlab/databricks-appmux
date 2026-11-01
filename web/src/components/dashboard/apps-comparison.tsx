import { comparisonCells } from "@/lib/dashboard/model";
import type { AppComparisonRow } from "@/lib/dashboard/series";
import { Button } from "@/components/button";
import { DataTable } from "@/components/data-table";
import type { DataTableColumn } from "@/components/data-table";

export interface AppsComparisonProps {
  rows: readonly AppComparisonRow[];
  /** Whether the person may open this app's charts (an app deleted since the rows were written is not in the directory). */
  isSelectable: (slug: string) => boolean;
  onSelectApp: (slug: string) => void;
}

/** Every app over the window in one table, busiest first; the slug is text, never a link built from the response. */
export function AppsComparison({
  rows,
  isSelectable,
  onSelectApp,
}: AppsComparisonProps) {
  const columns: DataTableColumn<AppComparisonRow>[] = [
    {
      key: "app",
      header: "App",
      isRowHeader: true,
      cell: (row) => (
        <span className="font-mono text-[0.9em] wrap-break-word">
          {row.slug}
        </span>
      ),
    },
    {
      key: "requests",
      header: "Requests",
      align: "end",
      cell: (row) => comparisonCells(row).requests,
    },
    {
      key: "errors",
      header: "Server errors",
      align: "end",
      cell: (row) => comparisonCells(row).serverErrors,
    },
    {
      key: "p50",
      header: "Latency p50",
      cell: (row) => comparisonCells(row).p50,
    },
    {
      key: "p95",
      header: "Latency p95",
      cell: (row) => comparisonCells(row).p95,
    },
    {
      key: "memory",
      header: "Memory peak",
      align: "end",
      cell: (row) => comparisonCells(row).memory,
    },
    {
      key: "egress",
      header: "Egress drops",
      align: "end",
      cell: (row) => comparisonCells(row).egressDrops,
    },
    {
      key: "restarts",
      header: "Restarts",
      align: "end",
      cell: (row) => comparisonCells(row).restarts,
    },
    {
      key: "charts",
      header: "Charts",
      cell: (row) =>
        isSelectable(row.slug) ? (
          <Button
            variant="ghost"
            size="small"
            onClick={() => {
              onSelectApp(row.slug);
            }}
          >
            View charts
            <span className="sr-only"> for {row.slug}</span>
          </Button>
        ) : null,
    },
  ];
  return (
    <DataTable
      caption="Apps compared over the window"
      isCaptionHidden
      layout="scroll"
      columns={columns}
      rows={rows}
      rowKey={(row) => row.slug}
    />
  );
}
