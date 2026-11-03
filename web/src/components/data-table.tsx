import { cn } from "cn";
import type { ReactNode } from "react";

import { EmptyState } from "./empty-state";
import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "./ui/table";

export interface DataTableColumn<Row> {
  /** Stable id of the column (a React key); not shown. */
  key: string;
  /** The column heading, plain text. */
  header: string;
  /**
   * What the cell shows. Return a string or elements: a string is escaped by
   * React, so server text never becomes markup. Do not build HTML strings.
   */
  cell: (row: Row) => ReactNode;
  /** `"end"` right-aligns numbers (in left-to-right text) and sets tabular digits. */
  align?: "end";
  /** The cell names its row (usually the first column: the slug, the title); it becomes a row header for screen readers. */
  isRowHeader?: boolean;
  /**
   * On a phone each cell of a stacked table is labelled with its column header.
   * Set this for a cell that needs no label, such as the buttons of the row.
   */
  isLabelHiddenWhenStacked?: boolean;
}

/**
 * How the table behaves on a narrow screen (below `sm`, 640 px):
 *  - `stacked` (default): each row becomes a block with the row header as its
 *    title and one labelled line per other cell, so nothing is clipped and no
 *    column is squeezed to one word per line.
 *  - `scroll`: the columns stay and the table scrolls sideways in its frame.
 *    For tables of numbers, whose point is to be read across.
 */
export type DataTableLayout = "stacked" | "scroll";

export interface DataTableProps<Row> {
  /** Names the table for screen readers and is shown above it. */
  caption: string;
  /** Hide the caption visually; it still names the table. Use when a heading above already says it. */
  isCaptionHidden?: boolean;
  columns: readonly DataTableColumn<Row>[];
  rows: readonly Row[];
  /** A stable id per row (a slug, a release id), not the index. */
  rowKey: (row: Row) => string;
  /** Shown instead of the table when `rows` is empty. Default: an `EmptyState` titled "Nothing to show". */
  empty?: ReactNode;
  layout?: DataTableLayout;
}

// Right-aligned numbers also get fixed-width digits, so columns of them line up.
function alignClass(column: { align?: "end" }): string | undefined {
  return column.align === "end" ? "text-end tabular-nums" : undefined;
}

// A cell that may hold a long value (an email, a path) breaks it only when it
// cannot fit on its own line (`wrap-break-word`), so the column's minimum width
// is its longest word and an address is never split in the middle of a table
// that has room. (`wrap-break-word` would count every character as a break and
// squeeze a column to one letter, seen at 390 px.) A floor per column count
// still makes a narrow screen scroll the table sideways (`scroll` layout; a
// stacked table has no columns to protect on a phone and applies it from `sm`).
// Literal class names: Tailwind only emits what it can read in the source. Two
// columns fit a phone as they are.
function minWidthClass(
  columnCount: number,
  layout: DataTableLayout,
): string | undefined {
  const isStacked = layout === "stacked";
  if (columnCount >= 6) return isStacked ? "sm:min-w-[48rem]" : "min-w-[48rem]";
  if (columnCount >= 4) return isStacked ? "sm:min-w-[32rem]" : "min-w-[32rem]";
  if (columnCount === 3)
    return isStacked ? "sm:min-w-[20rem]" : "min-w-[20rem]";
  return undefined;
}

// The phone layout of a stacked table. Every part is drawn as a block, so the
// table roles are written out: a table whose `display` is overridden is no
// longer a table to some browsers' accessibility tree. The labels are CSS
// generated content (`data-label`), so they are never part of the cell's text.
const STACKED = {
  table: "max-sm:block",
  // A caption inside a block table would shrink to its own width and wrap in a few letters per line.
  caption: "max-sm:block",
  header: "max-sm:sr-only",
  body: "max-sm:block",
  row: "max-sm:block max-sm:px-3 max-sm:py-3",
  rowHeader:
    "max-sm:block max-sm:px-0 max-sm:pt-0 max-sm:pb-1 max-sm:text-base",
  cell: "max-sm:grid max-sm:grid-cols-[minmax(0,6.5rem)_minmax(0,1fr)] max-sm:gap-x-3 max-sm:px-0 max-sm:py-1 max-sm:text-start max-sm:before:font-semibold max-sm:before:text-muted-foreground max-sm:before:content-[attr(data-label)]",
  unlabelledCell: "max-sm:block max-sm:px-0 max-sm:py-1 max-sm:empty:hidden",
} as const;

/** A table whose columns are typed over the row, so a cell cannot read a field the row lacks. */
export function DataTable<Row>({
  caption,
  isCaptionHidden = false,
  columns,
  rows,
  rowKey,
  empty,
  layout = "stacked",
}: DataTableProps<Row>) {
  if (rows.length === 0) {
    return <>{empty ?? <EmptyState title="Nothing to show" />}</>;
  }
  const isStacked = layout === "stacked";

  return (
    <Table
      frameLabel={caption}
      role={isStacked ? "table" : undefined}
      className={cn(
        minWidthClass(columns.length, layout),
        isStacked && STACKED.table,
      )}
    >
      {/* A hidden caption is a bare element: the visible one's padding would fight `sr-only`. */}
      {isCaptionHidden ? (
        <caption className="sr-only">{caption}</caption>
      ) : (
        <TableCaption className={cn(isStacked && STACKED.caption)}>
          {caption}
        </TableCaption>
      )}
      <TableHeader
        role={isStacked ? "rowgroup" : undefined}
        className={cn(isStacked && STACKED.header)}
      >
        <TableRow role={isStacked ? "row" : undefined}>
          {columns.map((column) => (
            <TableHead
              key={column.key}
              scope="col"
              role={isStacked ? "columnheader" : undefined}
              className={alignClass(column)}
            >
              {column.header}
            </TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody
        role={isStacked ? "rowgroup" : undefined}
        className={cn(isStacked && STACKED.body)}
      >
        {rows.map((row) => (
          <TableRow
            key={rowKey(row)}
            role={isStacked ? "row" : undefined}
            className={cn(isStacked && STACKED.row)}
          >
            {columns.map((column) =>
              column.isRowHeader === true ? (
                <TableHead
                  key={column.key}
                  scope="row"
                  role={isStacked ? "rowheader" : undefined}
                  className={cn(
                    "text-sm font-semibold whitespace-normal text-foreground",
                    alignClass(column),
                    isStacked && STACKED.rowHeader,
                  )}
                >
                  {column.cell(row)}
                </TableHead>
              ) : (
                <TableCell
                  key={column.key}
                  role={isStacked ? "cell" : undefined}
                  data-label={
                    isStacked && column.isLabelHiddenWhenStacked !== true
                      ? column.header
                      : undefined
                  }
                  className={cn(
                    alignClass(column),
                    isStacked &&
                      (column.isLabelHiddenWhenStacked === true
                        ? STACKED.unlabelledCell
                        : STACKED.cell),
                  )}
                >
                  {column.cell(row)}
                </TableCell>
              ),
            )}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
