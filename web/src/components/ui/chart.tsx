"use client";

import { cn } from "cn";
import { useId } from "react";
import type { ComponentProps, ReactElement } from "react";
import { ResponsiveContainer, Tooltip } from "recharts";
import type { TooltipContentProps } from "recharts";

import { useIsClient } from "@/hooks/use-is-client";
import { Skeleton } from "./skeleton";

// shadcn's chart, changed for the strict CSP the edge serves the admin UI under
// (`style-src 'self'`: no `style` attribute, no runtime `<style>`):
//  - `ChartStyle` is gone. It wrote the series colours into a `<style>` element
//    built with `dangerouslySetInnerHTML` and read them back as `--color-<key>`.
//    Here a series takes its colour from the fixed `--chart-1..6` tokens of
//    globals.css, written straight into the SVG as a presentation attribute
//    (`stroke="var(--chart-1)"`), which is not a style attribute.
//  - The tooltip and legend swatches took their colour from a `style` prop. The
//    tooltip here is a text card; the legend is drawn by the caller.
//  - Recharts measures the DOM and writes `style` attributes (the responsive
//    container, bars, the tooltip), so nothing of it may be in the prerendered
//    HTML. `ChartContainer` renders a placeholder of the chart's height until
//    the browser has taken over. (Recharts' own writes after that go through
//    the CSSOM, which the CSP allows.)
//  - A chart is one picture to a screen reader, with a name and a summary; the
//    caller must give the numbers again as a table.

/** Tall enough for the plot and both axes; override with `className` (a Tailwind height). */
const DEFAULT_HEIGHT_CLASS = "h-60";

export interface ChartContainerProps extends Omit<
  ComponentProps<"div">,
  "children" | "role" | "aria-label" | "aria-labelledby" | "aria-describedby"
> {
  /** The chart's name for screen readers ("Requests per minute"). */
  label: string;
  /** One sentence saying what the picture shows; read after the name. */
  description: string;
  /** One Recharts chart (`<BarChart>`, `<LineChart>`); it gets the container's width and height. */
  children: ReactElement;
}

function ChartContainer({
  label,
  description,
  className,
  children,
  ...props
}: ChartContainerProps) {
  const descriptionId = useId();
  const isClient = useIsClient();
  return (
    <>
      <p id={descriptionId} className="sr-only">
        {description}
      </p>
      <div
        data-slot="chart"
        role="img"
        aria-label={label}
        aria-describedby={descriptionId}
        className={cn("w-full text-xs", DEFAULT_HEIGHT_CLASS, className)}
        {...props}
      >
        {isClient ? (
          <ResponsiveContainer width="100%" height="100%">
            {children}
          </ResponsiveContainer>
        ) : (
          <Skeleton className="size-full" />
        )}
      </div>
    </>
  );
}

const ChartTooltip = Tooltip;

type ChartTooltipContentProps = Partial<
  Pick<TooltipContentProps, "active" | "label">
> & {
  /** The text of each column, by the column's index (the chart's category axis value). */
  tips: readonly string[];
  className?: string;
};

/**
 * The tooltip's card: one line of text for the hovered column. The text is the
 * model's (`ChartSpec.tips`), which reads the same as the chart's table row.
 * It is a mouse aid only; a keyboard or screen reader user has the table.
 */
function ChartTooltipContent({
  active,
  label,
  tips,
  className,
}: ChartTooltipContentProps) {
  const tip = typeof label === "number" ? tips[label] : undefined;
  if (active !== true || tip === undefined) return null;
  return (
    <div
      data-slot="chart-tooltip"
      className={cn(
        "max-w-xs rounded-md border bg-popover px-3 py-2 text-xs text-popover-foreground shadow-md",
        className,
      )}
    >
      {tip}
    </div>
  );
}

export { ChartContainer, ChartTooltip, ChartTooltipContent };
