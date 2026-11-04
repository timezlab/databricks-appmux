"use client";

import { cn } from "cn";
import type { ComponentProps } from "react";

// shadcn new-york table, with the appmux look: a framed table whose caption
// is its title (on top), quiet column headers (13 px, medium, muted, sentence
// case: the design's Type section), cells that wrap and align to the middle, so a row with a button in one cell keeps its text on the
// button's line. No opacity-modified colours: hover uses the `muted` token.
//
// The frame scrolls sideways when the table is wider than the screen, so it is
// a focusable, named region: without that a keyboard user cannot scroll it
// (TD-011). `frameLabel` is the name; DataTable passes its caption.
function Table({
  className,
  frameLabel,
  ...props
}: ComponentProps<"table"> & { frameLabel: string }) {
  return (
    <div
      data-slot="table-container"
      role="region"
      aria-label={frameLabel}
      tabIndex={0}
      className="relative w-full overflow-x-auto rounded-md border"
    >
      <table
        data-slot="table"
        className={cn("w-full caption-top border-collapse text-sm", className)}
        {...props}
      />
    </div>
  );
}

function TableHeader({ className, ...props }: ComponentProps<"thead">) {
  return (
    <thead
      data-slot="table-header"
      className={cn("bg-card", className)}
      {...props}
    />
  );
}

function TableBody({ className, ...props }: ComponentProps<"tbody">) {
  return (
    <tbody
      data-slot="table-body"
      className={cn("[&_tr:last-child]:border-b-0", className)}
      {...props}
    />
  );
}

function TableFooter({ className, ...props }: ComponentProps<"tfoot">) {
  return (
    <tfoot
      data-slot="table-footer"
      className={cn("border-t bg-card font-medium", className)}
      {...props}
    />
  );
}

function TableRow({ className, ...props }: ComponentProps<"tr">) {
  return (
    <tr
      data-slot="table-row"
      className={cn("border-b transition-colors hover:bg-muted", className)}
      {...props}
    />
  );
}

function TableHead({ className, ...props }: ComponentProps<"th">) {
  return (
    <th
      data-slot="table-head"
      className={cn(
        "px-3 py-2 text-start align-middle text-xs font-medium whitespace-nowrap text-muted-foreground",
        className,
      )}
      {...props}
    />
  );
}

function TableCell({ className, ...props }: ComponentProps<"td">) {
  return (
    <td
      data-slot="table-cell"
      className={cn("px-3 py-2 align-middle", className)}
      {...props}
    />
  );
}

function TableCaption({ className, ...props }: ComponentProps<"caption">) {
  return (
    <caption
      data-slot="table-caption"
      className={cn(
        "px-3 py-2 text-start font-semibold text-foreground",
        className,
      )}
      {...props}
    />
  );
}

export {
  Table,
  TableHeader,
  TableBody,
  TableFooter,
  TableHead,
  TableRow,
  TableCell,
  TableCaption,
};
