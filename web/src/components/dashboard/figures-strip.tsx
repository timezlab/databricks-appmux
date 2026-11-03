import { cn } from "cn";
import {
  ArrowDownRightIcon,
  ArrowRightIcon,
  ArrowUpRightIcon,
} from "lucide-react";

import { SkeletonFigure } from "@/components/skeletons";
import { sparklinePath } from "@/lib/dashboard/figures";
import type { Figure, FigureChange } from "@/lib/dashboard/figures";

/** The sparkline's drawing box; the SVG stretches it to the figure's width. */
const SPARK_WIDTH = 120;
const SPARK_HEIGHT = 28;
/** As many placeholders as `buildFigures` gives an app, and the platform (which adds the apps running). */
const APP_FIGURES = 6;
const PLATFORM_FIGURES = 7;

/**
 * The cells sit in a grid that overhangs its frame by a pixel on each side, so
 * only the hairlines between cells show, and a short last row still has a line
 * above and beside it. The frame is a size container: the app's strip sits
 * beside the sidebar (which folds away) and the platform's does not, so the
 * columns follow the strip's own width, not the window's.
 */
const STRIP_FRAME = "@container overflow-hidden rounded-lg border bg-card";
const STRIP_GRID = "-m-px grid";
const STRIP_CELL = "flex min-w-0 flex-col gap-1 border-r border-b p-4";

/**
 * Column counts that fill whole rows: an app's 6 figures as 2 or 3 a row, or all
 * 6 in one; the platform's 7 as 2 or 4 a row, or all 7 in one once each cell is
 * about 10 rem wide (a count in the millions still fits).
 */
const APP_COLUMNS = "grid-cols-2 @xl:grid-cols-3 @5xl:grid-cols-6";
const PLATFORM_COLUMNS = "grid-cols-2 @xl:grid-cols-4 @min-[68rem]:grid-cols-7";

function columnsFor(count: number): string {
  return count > APP_FIGURES ? PLATFORM_COLUMNS : APP_COLUMNS;
}

function Sparkline({ values }: { values: readonly (number | null)[] }) {
  const path = sparklinePath(values, SPARK_WIDTH, SPARK_HEIGHT);
  return (
    <svg
      viewBox={`0 0 ${SPARK_WIDTH} ${SPARK_HEIGHT}`}
      preserveAspectRatio="none"
      aria-hidden="true"
      focusable="false"
      className="mt-1 h-7 w-full overflow-visible"
    >
      {path === "" ? null : (
        <path
          d={path}
          fill="none"
          className="stroke-chart-1"
          strokeWidth={2}
          strokeLinecap="round"
          strokeLinejoin="round"
          vectorEffect="non-scaling-stroke"
        />
      )}
    </svg>
  );
}

const CHANGE_ICONS = {
  up: ArrowUpRightIcon,
  down: ArrowDownRightIcon,
  same: ArrowRightIcon,
} as const;

/** The change in words; the arrow repeats the direction and is never its only carrier, nor coloured as good or bad. */
function Change({ change }: { change: FigureChange }) {
  const Icon = CHANGE_ICONS[change.direction];
  return (
    <p className="flex items-start gap-1 text-xs text-muted-foreground">
      <Icon className="mt-px size-3.5 shrink-0" aria-hidden="true" />
      <span>{change.text}</span>
    </p>
  );
}

/**
 * The figures over the window as one strip, divided by hairlines: each a
 * number in tabular digits with its unit, what it means, a sparkline of the
 * window and, where there was a window before, how it compares.
 */
export function FiguresStrip({ figures }: { figures: readonly Figure[] }) {
  return (
    <div className={STRIP_FRAME}>
      <dl className={cn(STRIP_GRID, columnsFor(figures.length))}>
        {figures.map((figure) => (
          <div key={figure.key} className={STRIP_CELL}>
            <dt className="text-sm text-muted-foreground">{figure.label}</dt>
            <dd className="flex flex-wrap items-baseline gap-x-1">
              <span className="text-2xl leading-8 font-semibold tabular-nums">
                {figure.value}
              </span>
              {figure.unit === null ? null : (
                <span className="text-sm text-muted-foreground">
                  {figure.unit}
                </span>
              )}
            </dd>
            {figure.detail === null ? null : (
              <dd className="text-xs text-muted-foreground">{figure.detail}</dd>
            )}
            {/* `mt-auto`: the sparklines and the changes of a row line up, however many lines the texts above take. */}
            <dd className="mt-auto">
              <Sparkline values={figure.spark} />
              <span className="sr-only">{figure.sparkText}</span>
            </dd>
            {figure.change === null ? null : (
              <dd>
                <Change change={figure.change} />
              </dd>
            )}
          </div>
        ))}
      </dl>
    </div>
  );
}

/** The strip's shape while the figures load; hidden from screen readers (the panel says what is loading). */
export function FiguresStripSkeleton({ isPlatform }: { isPlatform: boolean }) {
  const count = isPlatform ? PLATFORM_FIGURES : APP_FIGURES;
  return (
    <div className={STRIP_FRAME} aria-hidden="true" data-skeleton="figures">
      <div className={cn(STRIP_GRID, columnsFor(count))}>
        {Array.from({ length: count }, (_, index) => (
          // `*:min-w-0` lets the placeholder shrink with a narrow cell, as the figure it stands for does.
          <div key={index} className={cn(STRIP_CELL, "*:min-w-0")}>
            <SkeletonFigure />
          </div>
        ))}
      </div>
    </div>
  );
}
