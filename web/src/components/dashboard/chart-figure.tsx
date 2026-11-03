import { DataTable } from "@/components/data-table";
import type { DataTableColumn } from "@/components/data-table";
import { axisCaption } from "@/lib/dashboard/chart-data";
import type { ChartSpec, ChartTable } from "@/lib/dashboard/model";
import { ChartLegend } from "./chart-legend";
import { ColumnChart } from "./column-chart";
import { LineChart } from "./line-chart";

type TableRow = ChartTable["rows"][number];

/** The first column names the row (the time or the bucket); the rest are numbers. */
function tableColumns(headers: readonly string[]): DataTableColumn<TableRow>[] {
  return headers.map((header, index) => ({
    key: String(index),
    header,
    cell: (row: TableRow) => row.cells[index] ?? "",
    isRowHeader: index === 0,
    align: index === 0 ? undefined : "end",
  }));
}

/**
 * One chart with what makes it readable without colour or a mouse: a legend
 * when there are two or more series, the picture, the caption of its time
 * axis, and the same figures as a table in a `<details>`. A chart with nothing
 * to draw shows its message.
 */
export function ChartFigure({ spec }: { spec: ChartSpec }) {
  return (
    <figure className="m-0 flex min-w-0 flex-col gap-4">
      {spec.emptyMessage === null ? (
        <>
          <ChartLegend spec={spec} />
          {/* On a narrow screen the chart keeps a readable width and scrolls sideways; a focusable, named region lets the keyboard scroll it (TD-011). */}
          <div
            role="region"
            aria-label={`${spec.title}, chart`}
            tabIndex={0}
            className="overflow-x-auto"
          >
            {spec.kind === "columns" ? (
              <ColumnChart spec={spec} />
            ) : (
              <LineChart spec={spec} />
            )}
          </div>
          <p className="text-center text-xs text-muted-foreground">
            {axisCaption(spec)}
          </p>
          <details className="text-sm">
            <summary className="w-fit cursor-pointer font-medium">
              Show the values as a table
            </summary>
            <div className="mt-2 max-h-96 overflow-auto">
              <DataTable
                caption={`${spec.title}, as a table`}
                isCaptionHidden
                layout="scroll"
                columns={tableColumns(spec.table.headers)}
                rows={spec.table.rows}
                rowKey={(row) => row.key}
              />
            </div>
          </details>
        </>
      ) : (
        <p className="text-muted-foreground">{spec.emptyMessage}</p>
      )}
    </figure>
  );
}
