import { cn } from "cn";

import { Skeleton } from "./ui/skeleton";

/*
 * Placeholders with the shape of what will arrive, so the page does not jump
 * when the data lands (docs/product/admin-ui-design.md, Skeletons). They are
 * hidden from screen readers: put a `Loading` (the spinner and the words, in a
 * status region) beside them, which says what is loading.
 */

/** The widths of successive lines, so a block of text does not look like a bar chart. */
const LINE_WIDTHS = ["w-3/4", "w-full", "w-5/6", "w-2/3", "w-11/12"] as const;

export interface SkeletonLinesProps {
  /** How many lines of text to stand in for. Default 3. */
  lines?: number;
}

export function SkeletonLines({ lines = 3 }: SkeletonLinesProps) {
  return (
    <div
      aria-hidden="true"
      className="flex w-full max-w-xl flex-col gap-2"
      data-skeleton="lines"
    >
      {Array.from({ length: lines }, (_, index) => (
        <Skeleton
          key={index}
          className={cn("h-4", LINE_WIDTHS[index % LINE_WIDTHS.length])}
        />
      ))}
    </div>
  );
}

export interface SkeletonTableProps {
  /** Body rows. Default 4. */
  rows?: number;
  /** Columns. Default 3. */
  columns?: number;
}

const GRID_COLUMNS: Record<number, string> = {
  1: "grid-cols-1",
  2: "grid-cols-2",
  3: "grid-cols-3",
  4: "grid-cols-4",
  5: "grid-cols-5",
  6: "grid-cols-6",
};

export function SkeletonTable({ rows = 4, columns = 3 }: SkeletonTableProps) {
  const gridColumns = GRID_COLUMNS[columns] ?? "grid-cols-6";
  const cells = (isHeader: boolean) =>
    Array.from({ length: columns }, (_, column) => (
      <Skeleton
        key={column}
        className={cn(
          isHeader ? "h-3" : "h-4",
          column === 0 ? "w-2/3" : "w-1/2",
        )}
      />
    ));
  return (
    <div
      aria-hidden="true"
      className="flex w-full flex-col overflow-hidden rounded-control border"
      data-skeleton="table"
    >
      <div className={cn("grid gap-4 border-b px-3 py-2.5", gridColumns)}>
        {cells(true)}
      </div>
      {Array.from({ length: rows }, (_, row) => (
        <div
          key={row}
          className={cn(
            "grid gap-4 px-3 py-3.5",
            gridColumns,
            row < rows - 1 && "border-b",
          )}
        >
          {cells(false)}
        </div>
      ))}
    </div>
  );
}

/** One figure of the dashboard strip: a label, a number and a sparkline. */
export function SkeletonFigure() {
  return (
    <div
      aria-hidden="true"
      className="flex min-w-40 flex-col gap-2"
      data-skeleton="figure"
    >
      <Skeleton className="h-3 w-24" />
      <Skeleton className="h-7 w-16" />
      <Skeleton className="h-8 w-full" />
    </div>
  );
}
