"use client";

import { useId } from "react";
import type { ReactNode } from "react";

import { Skeleton } from "@/components/ui/skeleton";
import type { ChartSpec } from "@/lib/dashboard/model";
import { ChartFigure } from "./chart-figure";
import { useColorScheme } from "./use-color-scheme";

/**
 * A chart's frame in each state of its answer: `loading` and `failed` before
 * there is a spec (the frame keeps the chart's place and says what it will
 * hold), `ready` with the spec, which draws the chart or, when the spec has an
 * `emptyMessage`, says there was nothing to draw.
 */
export type ChartFrameProps =
  | { status: "loading"; name: string }
  | { status: "failed"; name: string }
  | { status: "ready"; spec: ChartSpec; children?: ReactNode };

const FRAME_CLASS = "flex min-w-0 flex-col gap-4 rounded-lg border bg-card p-4";

/** The shape of a chart while it loads: a legend line, the plot and its caption. Hidden from screen readers: the panel's status says what is loading. */
function ChartSkeleton() {
  return (
    <div
      aria-hidden="true"
      className="flex flex-col gap-4"
      data-skeleton="chart"
    >
      <Skeleton className="h-4 w-48 max-w-full" />
      <Skeleton className="h-60 w-full" />
      <Skeleton className="mx-auto h-3 w-40 max-w-full" />
    </div>
  );
}

/**
 * The charts are keyed by the colour scheme: the bars and lines follow a theme
 * change through `var(--chart-N)` by themselves, but a browser may keep an SVG
 * texture pattern painted in the old colours until it is drawn again.
 */
function ReadyChart({ spec }: { spec: ChartSpec }) {
  const scheme = useColorScheme();
  return <ChartFigure key={scheme} spec={spec} />;
}

export interface PanelFrameProps {
  title: string;
  isBusy?: boolean;
  children: ReactNode;
}

/** A card with its `h3`: a chart, or a table of the dashboard, named as a group. */
export function PanelFrame({
  title,
  isBusy = false,
  children,
}: PanelFrameProps) {
  const headingId = useId();
  return (
    <div
      className={FRAME_CLASS}
      role="group"
      aria-labelledby={headingId}
      aria-busy={isBusy ? true : undefined}
    >
      <h3 id={headingId} className="text-base leading-6 font-semibold">
        {title}
      </h3>
      {children}
    </div>
  );
}

export function ChartFrame(props: ChartFrameProps) {
  const title = props.status === "ready" ? props.spec.title : props.name;
  return (
    <PanelFrame title={title} isBusy={props.status === "loading"}>
      {props.status === "loading" ? <ChartSkeleton /> : null}
      {props.status === "failed" ? (
        <p className="text-sm text-muted-foreground">
          Not shown: the metrics could not be read.
        </p>
      ) : null}
      {props.status === "ready" ? (
        <>
          <ReadyChart spec={props.spec} />
          {props.children}
        </>
      ) : null}
    </PanelFrame>
  );
}
